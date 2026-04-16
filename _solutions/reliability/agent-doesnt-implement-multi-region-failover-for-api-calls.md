---
title: "Agent Doesn't Implement Multi-Region Failover for API Calls"
description: "AI agents hardcode a single API endpoint region — when that region experiences an outage, elevated latency, or rate limit exhaustion, all requests fail rather than automatically routing to a healthy alternative region."
problem_description: |
  Production AI agents overwhelmingly target a single regional endpoint. When Anthropic's us-east-1 endpoint degrades, agents in Europe don't automatically shift to eu-west-1; agents hitting rate limits in one region don't overflow to another. The result is full outages that could have been partial degradations. Multi-region failover requires: health probing per region, latency-aware routing, automatic demotion of degraded regions, and gradual re-promotion after recovery — all without adding meaningful latency to the happy path.
category: reliability
difficulty: advanced
tags: [multi-region, failover, high-availability, routing, resilience]
---

## Solution 1: Priority-Ordered Regional Failover

Try regions in priority order — primary first, fallback second — with a configurable timeout before switching. Zero overhead on the happy path.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class Region:
    name: str
    base_url: str
    priority: int  # Lower = higher priority


REGIONS = [
    Region("us-east-1", "https://api.anthropic.com", priority=1),
    Region("eu-west-1", "https://api.eu.anthropic.com", priority=2),
    Region("us-west-2", "https://api.us-west.anthropic.com", priority=3),
]


class PriorityRegionalFailover:
    def __init__(
        self,
        regions: list[Region],
        per_region_timeout: float = 10.0,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
    ):
        self._regions = sorted(regions, key=lambda r: r.priority)
        self._timeout = per_region_timeout
        self.model = model
        self.max_tokens = max_tokens
        self._failover_count = 0

    async def _try_region(
        self,
        region: Region,
        system: str,
        user_message: str,
    ) -> str:
        client = AsyncAnthropic(base_url=region.base_url)
        response = await asyncio.wait_for(
            client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_message}],
            ),
            timeout=self._timeout,
        )
        return response.content[0].text

    async def complete(
        self,
        system: str,
        user_message: str,
    ) -> tuple[str, str]:
        """Returns (response_text, region_used)."""
        last_error = None

        for i, region in enumerate(self._regions):
            try:
                text = await self._try_region(region, system, user_message)
                if i > 0:
                    self._failover_count += 1
                    print(f"[failover] Succeeded on {region.name} (attempt {i+1})")
                return text, region.name
            except (asyncio.TimeoutError, Exception) as e:
                last_error = e
                print(f"[failover] {region.name} failed: {type(e).__name__}: {e}")
                continue

        raise RuntimeError(f"All {len(self._regions)} regions failed. Last error: {last_error}")

    @property
    def failover_count(self) -> int:
        return self._failover_count


# Usage
async def main():
    router = PriorityRegionalFailover(
        regions=REGIONS,
        per_region_timeout=8.0,
    )

    text, region = await router.complete(
        system="Answer in one sentence.",
        user_message="What is a CDN?",
    )
    print(f"Response from {region}: {text}")
    print(f"Total failovers: {router.failover_count}")

asyncio.run(main())
```

## Solution 2: Latency-Aware Least-Loaded Region Router

Measure response latency per region with a rolling window and always route to the fastest healthy region — proactively shifting traffic before failures occur.

```python
import asyncio
import statistics
import time
from anthropic import AsyncAnthropic
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RegionStats:
    name: str
    base_url: str
    latencies: deque = field(default_factory=lambda: deque(maxlen=20))
    error_count: int = 0
    success_count: int = 0
    last_error_at: float = 0.0

    @property
    def p50_latency(self) -> float:
        if not self.latencies:
            return 0.0
        return statistics.median(self.latencies)

    @property
    def error_rate(self) -> float:
        total = self.error_count + self.success_count
        return self.error_count / total if total > 0 else 0.0

    @property
    def health_score(self) -> float:
        """0.0 = unhealthy, 1.0 = perfectly healthy. Lower is better for routing."""
        if not self.latencies:
            return 999.0  # Unknown — deprioritize
        penalty = self.error_rate * 10.0
        return self.p50_latency + penalty

    def record_success(self, latency_ms: float):
        self.latencies.append(latency_ms)
        self.success_count += 1

    def record_error(self):
        self.error_count += 1
        self.last_error_at = time.time()
        # Add a high phantom latency to penalize this region
        self.latencies.append(30000.0)


class LatencyAwareRegionRouter:
    def __init__(
        self,
        regions: list[dict],  # [{"name": ..., "base_url": ...}]
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
        request_timeout: float = 15.0,
    ):
        self._stats = [
            RegionStats(name=r["name"], base_url=r["base_url"])
            for r in regions
        ]
        self.model = model
        self.max_tokens = max_tokens
        self._timeout = request_timeout

    def _best_region(self) -> RegionStats:
        return min(self._stats, key=lambda r: r.health_score)

    async def complete(
        self,
        system: str,
        user_message: str,
        fallback_on_error: bool = True,
    ) -> tuple[str, str]:
        # Try best region first, then fall back to others
        ordered = sorted(self._stats, key=lambda r: r.health_score)

        for region in ordered:
            start = time.monotonic()
            try:
                client = AsyncAnthropic(base_url=region.base_url)
                response = await asyncio.wait_for(
                    client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=system,
                        messages=[{"role": "user", "content": user_message}],
                    ),
                    timeout=self._timeout,
                )
                latency_ms = (time.monotonic() - start) * 1000
                region.record_success(latency_ms)
                return response.content[0].text, region.name

            except Exception as e:
                region.record_error()
                print(f"[router] {region.name} failed ({type(e).__name__}), trying next")
                if not fallback_on_error:
                    raise

        raise RuntimeError("All regions exhausted")

    def region_report(self) -> list[dict]:
        return [
            {
                "region": r.name,
                "p50_ms": round(r.p50_latency, 1),
                "error_rate": round(r.error_rate, 3),
                "health_score": round(r.health_score, 1),
                "requests": r.success_count + r.error_count,
            }
            for r in sorted(self._stats, key=lambda r: r.health_score)
        ]


# Usage
async def main():
    router = LatencyAwareRegionRouter(
        regions=[
            {"name": "us-east-1", "base_url": "https://api.anthropic.com"},
            {"name": "eu-west-1", "base_url": "https://api.eu.anthropic.com"},
        ],
        request_timeout=10.0,
    )

    for i in range(5):
        text, region = await router.complete("Answer briefly.", f"Question {i}?")
        print(f"[{region}] {text[:60]}")

    print(f"\nRegion report: {router.region_report()}")

asyncio.run(main())
```

## Solution 3: Circuit-Breaker Per Region with Automatic Recovery

Maintain an independent circuit breaker per region — open the circuit on repeated failures, then probe with a single request after a cooldown window before re-admitting traffic.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from enum import Enum
from collections import deque


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class RegionCircuit:
    name: str
    base_url: str
    state: CircuitState = CircuitState.CLOSED
    failure_window: deque = field(default_factory=lambda: deque(maxlen=10))
    opened_at: float = 0.0
    open_duration: float = 60.0
    failure_threshold: float = 0.5  # 50% failures → open

    @property
    def failure_rate(self) -> float:
        if not self.failure_window:
            return 0.0
        return sum(self.failure_window) / len(self.failure_window)

    def record_success(self):
        self.failure_window.append(0)
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.CLOSED
            print(f"[circuit:{self.name}] CLOSED — recovered")

    def record_failure(self):
        self.failure_window.append(1)
        if self.state == CircuitState.HALF_OPEN:
            self.state = CircuitState.OPEN
            self.opened_at = time.time()
            print(f"[circuit:{self.name}] REOPENED — probe failed")
        elif self.state == CircuitState.CLOSED and self.failure_rate >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.opened_at = time.time()
            print(f"[circuit:{self.name}] OPENED (failure_rate={self.failure_rate:.2f})")

    def allow_request(self) -> bool:
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.time() - self.opened_at
            if elapsed >= self.open_duration:
                self.state = CircuitState.HALF_OPEN
                print(f"[circuit:{self.name}] HALF_OPEN — probing")
                return True
            return False
        # HALF_OPEN: allow one probe at a time
        return True


class CircuitBreakerRegionRouter:
    def __init__(
        self,
        regions: list[dict],
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
        request_timeout: float = 10.0,
    ):
        self._circuits = [
            RegionCircuit(name=r["name"], base_url=r["base_url"])
            for r in regions
        ]
        self.model = model
        self.max_tokens = max_tokens
        self._timeout = request_timeout

    def _available_regions(self) -> list[RegionCircuit]:
        return [r for r in self._circuits if r.allow_request()]

    async def complete(self, system: str, user_message: str) -> tuple[str, str]:
        available = self._available_regions()

        if not available:
            raise RuntimeError("All regions circuit-broken")

        for region in available:
            try:
                client = AsyncAnthropic(base_url=region.base_url)
                response = await asyncio.wait_for(
                    client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=system,
                        messages=[{"role": "user", "content": user_message}],
                    ),
                    timeout=self._timeout,
                )
                region.record_success()
                return response.content[0].text, region.name
            except Exception as e:
                region.record_failure()
                print(f"[router] {region.name} error: {type(e).__name__}")
                continue

        raise RuntimeError("All available regions failed")

    def status(self) -> list[dict]:
        return [
            {
                "region": r.name,
                "state": r.state.value,
                "failure_rate": round(r.failure_rate, 2),
            }
            for r in self._circuits
        ]


# Usage
async def main():
    router = CircuitBreakerRegionRouter(
        regions=[
            {"name": "us-east-1", "base_url": "https://api.anthropic.com"},
            {"name": "eu-west-1", "base_url": "https://api.eu.anthropic.com"},
        ],
    )

    for i in range(8):
        try:
            text, region = await router.complete("Answer briefly.", f"What is HTTP/{i}?")
            print(f"[{region}] {text[:60]}")
        except RuntimeError as e:
            print(f"[FAILED] {e}")

    print(f"\nCircuit status: {router.status()}")

asyncio.run(main())
```

## Solution 4: Geolocation-Aware Region Selection

Select the closest region based on the agent's deployment location, falling back to next-closest on failure — minimizing latency while maintaining availability.

```python
import asyncio
import math
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass


@dataclass
class GeoRegion:
    name: str
    base_url: str
    lat: float
    lon: float


GEO_REGIONS = [
    GeoRegion("us-east-1", "https://api.anthropic.com", lat=39.0, lon=-77.0),
    GeoRegion("eu-west-1", "https://api.eu.anthropic.com", lat=53.3, lon=-6.3),
    GeoRegion("ap-northeast-1", "https://api.ap.anthropic.com", lat=35.7, lon=139.7),
]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def rank_regions_by_distance(
    agent_lat: float,
    agent_lon: float,
    regions: list[GeoRegion],
) -> list[tuple[GeoRegion, float]]:
    return sorted(
        [(r, haversine_km(agent_lat, agent_lon, r.lat, r.lon)) for r in regions],
        key=lambda x: x[1],
    )


class GeoAwareRegionRouter:
    def __init__(
        self,
        regions: list[GeoRegion],
        agent_lat: float,
        agent_lon: float,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
        request_timeout: float = 10.0,
    ):
        self._ranked = rank_regions_by_distance(agent_lat, agent_lon, regions)
        self.model = model
        self.max_tokens = max_tokens
        self._timeout = request_timeout
        self._errors: dict[str, int] = {}

        print("Region priority order:")
        for region, dist_km in self._ranked:
            print(f"  {region.name}: {dist_km:.0f} km")

    async def complete(self, system: str, user_message: str) -> tuple[str, str]:
        for region, dist_km in self._ranked:
            try:
                client = AsyncAnthropic(base_url=region.base_url)
                response = await asyncio.wait_for(
                    client.messages.create(
                        model=self.model,
                        max_tokens=self.max_tokens,
                        system=system,
                        messages=[{"role": "user", "content": user_message}],
                    ),
                    timeout=self._timeout,
                )
                return response.content[0].text, region.name
            except Exception as e:
                self._errors[region.name] = self._errors.get(region.name, 0) + 1
                print(f"[geo-router] {region.name} ({dist_km:.0f}km) failed: {e}")

        raise RuntimeError("All geo-ordered regions failed")


# Usage
async def main():
    # Agent deployed in Seoul, South Korea
    router = GeoAwareRegionRouter(
        regions=GEO_REGIONS,
        agent_lat=37.6,   # Seoul
        agent_lon=126.9,
    )

    text, region = await router.complete("Answer briefly.", "What is latency?")
    print(f"\nServed by: {region}")
    print(f"Response: {text[:100]}")

asyncio.run(main())
```

## Solution 5: Active Health Probing with Background Region Monitor

Run background health probes against all regions on a schedule — maintain a live health map used by the router rather than discovering failures reactively mid-request.

```python
import asyncio
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field


@dataclass
class RegionHealth:
    name: str
    base_url: str
    healthy: bool = True
    last_probe_ms: float = 0.0
    last_probe_at: float = 0.0
    consecutive_failures: int = 0
    consecutive_successes: int = 0


PROBE_MESSAGE = "Reply with: OK"
PROBE_SYSTEM = "Reply with only the word OK."


class ActiveHealthProber:
    def __init__(
        self,
        regions: list[dict],
        probe_interval: float = 30.0,
        probe_timeout: float = 5.0,
        unhealthy_threshold: int = 2,
        recovery_threshold: int = 1,
        model: str = "claude-haiku-4-5-20251001",
    ):
        self._regions = [
            RegionHealth(name=r["name"], base_url=r["base_url"])
            for r in regions
        ]
        self._probe_interval = probe_interval
        self._probe_timeout = probe_timeout
        self._unhealthy_threshold = unhealthy_threshold
        self._recovery_threshold = recovery_threshold
        self.model = model
        self._probe_task: asyncio.Task | None = None

    async def start(self):
        self._probe_task = asyncio.create_task(self._probe_loop())

    async def stop(self):
        if self._probe_task:
            self._probe_task.cancel()
            try:
                await self._probe_task
            except asyncio.CancelledError:
                pass

    async def _probe_region(self, region: RegionHealth):
        start = time.monotonic()
        try:
            client = AsyncAnthropic(base_url=region.base_url)
            await asyncio.wait_for(
                client.messages.create(
                    model=self.model,
                    max_tokens=10,
                    system=PROBE_SYSTEM,
                    messages=[{"role": "user", "content": PROBE_MESSAGE}],
                ),
                timeout=self._probe_timeout,
            )
            latency_ms = (time.monotonic() - start) * 1000
            region.last_probe_ms = latency_ms
            region.last_probe_at = time.time()
            region.consecutive_failures = 0
            region.consecutive_successes += 1

            if not region.healthy and region.consecutive_successes >= self._recovery_threshold:
                region.healthy = True
                print(f"[probe] {region.name} RECOVERED ({latency_ms:.0f}ms)")

        except Exception as e:
            region.consecutive_successes = 0
            region.consecutive_failures += 1
            region.last_probe_at = time.time()

            if region.healthy and region.consecutive_failures >= self._unhealthy_threshold:
                region.healthy = False
                print(f"[probe] {region.name} UNHEALTHY ({type(e).__name__})")

    async def _probe_loop(self):
        while True:
            await asyncio.gather(*[self._probe_region(r) for r in self._regions])
            await asyncio.sleep(self._probe_interval)

    def healthy_regions(self) -> list[RegionHealth]:
        return [r for r in self._regions if r.healthy]

    async def complete(
        self,
        client_factory,
        system: str,
        user_message: str,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
    ) -> tuple[str, str]:
        candidates = sorted(self.healthy_regions(), key=lambda r: r.last_probe_ms)

        if not candidates:
            # All unhealthy — try all anyway
            candidates = sorted(self._regions, key=lambda r: r.consecutive_failures)

        for region in candidates:
            try:
                client = AsyncAnthropic(base_url=region.base_url)
                response = await client.messages.create(
                    model=model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user_message}],
                )
                return response.content[0].text, region.name
            except Exception:
                continue

        raise RuntimeError("No region could serve the request")

    def health_report(self) -> list[dict]:
        return [
            {
                "region": r.name,
                "healthy": r.healthy,
                "last_probe_ms": round(r.last_probe_ms, 1),
                "consecutive_failures": r.consecutive_failures,
            }
            for r in self._regions
        ]


# Usage
async def main():
    prober = ActiveHealthProber(
        regions=[
            {"name": "us-east-1", "base_url": "https://api.anthropic.com"},
            {"name": "eu-west-1", "base_url": "https://api.eu.anthropic.com"},
        ],
        probe_interval=60.0,
        probe_timeout=5.0,
    )

    await prober.start()

    # Give probes a moment to run
    await asyncio.sleep(2.0)

    print(f"Health report: {prober.health_report()}")

    text, region = await prober.complete(
        None, "Answer briefly.", "What is DNS?"
    )
    print(f"Served by {region}: {text[:80]}")

    await prober.stop()

asyncio.run(main())
```

## Solution 6: Weighted Traffic Splitting Across Regions

Split traffic across multiple healthy regions by weight — enabling gradual regional rollouts, cost optimization across regions with different pricing, and load distribution without hard failover semantics.

```python
import asyncio
import random
import time
from anthropic import AsyncAnthropic
from dataclasses import dataclass, field
from collections import defaultdict


@dataclass
class WeightedRegion:
    name: str
    base_url: str
    weight: float  # Relative traffic share
    max_rps: float = 100.0  # Optional RPS cap
    _request_count: int = field(default=0, init=False)
    _error_count: int = field(default=0, init=False)
    _total_latency_ms: float = field(default=0.0, init=False)

    def record(self, latency_ms: float, error: bool = False):
        self._request_count += 1
        if error:
            self._error_count += 1
        else:
            self._total_latency_ms += latency_ms

    @property
    def avg_latency(self) -> float:
        served = self._request_count - self._error_count
        return self._total_latency_ms / max(served, 1)

    @property
    def error_rate(self) -> float:
        return self._error_count / max(self._request_count, 1)


class WeightedRegionRouter:
    def __init__(
        self,
        regions: list[WeightedRegion],
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
        request_timeout: float = 10.0,
        auto_reweight: bool = True,
        reweight_interval: float = 60.0,
    ):
        self._regions = regions
        self.model = model
        self.max_tokens = max_tokens
        self._timeout = request_timeout
        self._auto_reweight = auto_reweight
        self._reweight_interval = reweight_interval
        self._last_reweight = time.time()

    def _select_region(self) -> WeightedRegion:
        total = sum(r.weight for r in self._regions)
        pick = random.uniform(0, total)
        cumulative = 0.0
        for region in self._regions:
            cumulative += region.weight
            if pick <= cumulative:
                return region
        return self._regions[-1]

    def _reweight(self):
        """Reduce weight for high-error-rate regions."""
        if not self._auto_reweight:
            return
        if time.time() - self._last_reweight < self._reweight_interval:
            return

        for region in self._regions:
            if region.error_rate > 0.2:
                old_weight = region.weight
                region.weight = max(region.weight * 0.5, 0.05)
                print(f"[reweight] {region.name}: {old_weight:.2f} → {region.weight:.2f} (error_rate={region.error_rate:.2f})")
            elif region.error_rate < 0.05 and region.weight < 1.0:
                region.weight = min(region.weight * 1.2, 1.0)

        self._last_reweight = time.time()

    async def complete(self, system: str, user_message: str) -> tuple[str, str]:
        self._reweight()
        region = self._select_region()
        start = time.monotonic()

        try:
            client = AsyncAnthropic(base_url=region.base_url)
            response = await asyncio.wait_for(
                client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user_message}],
                ),
                timeout=self._timeout,
            )
            latency_ms = (time.monotonic() - start) * 1000
            region.record(latency_ms)
            return response.content[0].text, region.name

        except Exception as e:
            region.record(0, error=True)
            # Fallback to any other region
            for fallback in self._regions:
                if fallback.name != region.name:
                    try:
                        client = AsyncAnthropic(base_url=fallback.base_url)
                        response = await asyncio.wait_for(
                            client.messages.create(
                                model=self.model,
                                max_tokens=self.max_tokens,
                                system=system,
                                messages=[{"role": "user", "content": user_message}],
                            ),
                            timeout=self._timeout,
                        )
                        latency_ms = (time.monotonic() - start) * 1000
                        fallback.record(latency_ms)
                        return response.content[0].text, fallback.name
                    except Exception:
                        fallback.record(0, error=True)

            raise RuntimeError("All regions failed")

    def distribution_report(self) -> list[dict]:
        total_requests = sum(r._request_count for r in self._regions)
        return [
            {
                "region": r.name,
                "weight": round(r.weight, 3),
                "requests": r._request_count,
                "share": round(r._request_count / max(total_requests, 1), 3),
                "avg_latency_ms": round(r.avg_latency, 1),
                "error_rate": round(r.error_rate, 3),
            }
            for r in self._regions
        ]


# Usage
async def main():
    router = WeightedRegionRouter(
        regions=[
            WeightedRegion("us-east-1", "https://api.anthropic.com", weight=0.7),
            WeightedRegion("eu-west-1", "https://api.eu.anthropic.com", weight=0.3),
        ],
    )

    tasks = [
        router.complete("Answer briefly.", f"Q{i}: what is caching?")
        for i in range(10)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    region_hits: dict[str, int] = defaultdict(int)
    for r in results:
        if isinstance(r, tuple):
            region_hits[r[1]] += 1

    print(f"Region hits: {dict(region_hits)}")
    print(f"Distribution: {router.distribution_report()}")

asyncio.run(main())
```

## Comparison

| Approach | Latency Impact | Failure Detection | Traffic Control | Complexity | Best For |
|---|---|---|---|---|---|
| Priority Failover | None (happy path) | Reactive | None | Low | Simple primary/backup setup |
| Latency-Aware Router | Minimal | Reactive | Automatic | Medium | Latency-sensitive production |
| Circuit Breaker | None (healthy) | Reactive + recovery | Automatic | Medium | Protecting against cascading failures |
| Geo-Aware Selection | Minimal | Reactive | Distance-based | Low | Globally distributed agents |
| Active Health Probing | None | Proactive | Automatic | High | SLA-critical workloads |
| Weighted Traffic Split | None | Reactive + auto | Configurable | Medium | Multi-region load distribution |
