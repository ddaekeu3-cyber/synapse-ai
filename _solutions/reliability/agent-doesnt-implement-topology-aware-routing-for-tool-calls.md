---
title: "Agent Doesn't Implement Topology-Aware Routing for Tool Calls"
description: "AI agents that route all tool calls to a fixed endpoint ignore network topology: cloud availability zones, data-center proximity, and latency heterogeneity. Topology-aware routing sends each request to the nearest healthy endpoint, cutting latency by 30–70% and improving resilience."
date: 2025-02-04
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-topology-aware-routing-for-tool-calls
tags:
  - topology-aware-routing
  - latency
  - multi-az
  - proximity
  - reliability
  - load-balancing
  - networking
symptoms:
  - "Tool calls from a US-east agent always go to a US-west database, adding 80 ms"
  - "Single AZ failure takes down all tool endpoints even though replicas exist in other AZs"
  - "Agent deployed in three regions but all traffic routes to one region's API"
  - "Cross-region data transfer costs are high despite regional replicas being available"
  - "Latency percentiles vary wildly across cloud regions with no routing logic to compensate"
---

## Problem

Most agent frameworks treat all tool endpoints as equivalent and pick one at random or round-robin. In reality, endpoints differ by:

- **Network distance**: an agent in `us-east-1` reaches `us-east-1a` endpoints in <1 ms but `us-west-2` endpoints in 60–80 ms.
- **Health**: a specific AZ or region may be degraded while others are healthy.
- **Capacity**: some replicas may be overloaded while others are idle.

Topology-aware routing encodes these relationships and steers traffic to the best available endpoint considering all three dimensions simultaneously.

---

## Solution 1: Latency-Based Endpoint Selector

Measure round-trip time to each endpoint and always prefer the fastest healthy one. Uses an exponential moving average to track latency without being fooled by transient spikes.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class EndpointStats:
    url: str
    region: str
    az: str
    ema_latency_ms: float = 100.0    # exponential moving average
    error_count: int = 0
    success_count: int = 0
    healthy: bool = True
    last_probe: float = 0.0

    @property
    def error_rate(self) -> float:
        total = self.success_count + self.error_count
        return self.error_count / max(1, total)

    def update_latency(self, latency_ms: float, alpha: float = 0.1):
        self.ema_latency_ms = alpha * latency_ms + (1 - alpha) * self.ema_latency_ms

    def effective_latency(self) -> float:
        """Penalty-adjusted latency: unhealthy endpoints get 10× penalty."""
        if not self.healthy or self.error_rate > 0.5:
            return self.ema_latency_ms * 10
        return self.ema_latency_ms


class LatencyBasedRouter:
    """
    Routes requests to the endpoint with the lowest effective latency.
    Probes endpoints periodically and updates EMA latency.

    Usage:
        router = LatencyBasedRouter([
            EndpointStats("https://api-east.svc", "us-east-1", "us-east-1a"),
            EndpointStats("https://api-west.svc", "us-west-2", "us-west-2b"),
        ])
        asyncio.create_task(router.probe_loop())

        endpoint = router.pick()
        result = await call(endpoint.url)
        router.record(endpoint.url, latency_ms=45.0, success=True)
    """

    def __init__(self, endpoints: List[EndpointStats],
                 probe_interval: float = 10.0):
        self._endpoints = {ep.url: ep for ep in endpoints}
        self._probe_interval = probe_interval

    def pick(self, preferred_region: Optional[str] = None) -> Optional[EndpointStats]:
        candidates = [e for e in self._endpoints.values() if e.healthy]
        if not candidates:
            # Fallback: any endpoint
            candidates = list(self._endpoints.values())
        if not candidates:
            return None
        # Region preference: if preferred_region matches, halve effective latency
        def score(ep: EndpointStats) -> float:
            lat = ep.effective_latency()
            if preferred_region and ep.region == preferred_region:
                lat *= 0.5
            return lat
        return min(candidates, key=score)

    def record(self, url: str, latency_ms: float, success: bool):
        ep = self._endpoints.get(url)
        if ep is None:
            return
        ep.update_latency(latency_ms)
        if success:
            ep.success_count += 1
        else:
            ep.error_count += 1
        ep.healthy = ep.error_rate < 0.8

    async def probe_loop(self, http_client=None):
        while True:
            await asyncio.sleep(self._probe_interval)
            for ep in list(self._endpoints.values()):
                await self._probe(ep, http_client)

    async def _probe(self, ep: EndpointStats, http_client=None):
        if http_client is None:
            return
        t0 = time.monotonic()
        try:
            await http_client.get(ep.url + "/health", timeout=2.0)
            ms = (time.monotonic() - t0) * 1000
            self.record(ep.url, ms, success=True)
            ep.last_probe = time.time()
        except Exception:
            self.record(ep.url, 5000.0, success=False)

    def stats(self) -> List[dict]:
        return [
            {
                "url": ep.url,
                "region": ep.region,
                "ema_latency_ms": round(ep.ema_latency_ms, 1),
                "healthy": ep.healthy,
                "error_rate": round(ep.error_rate, 3),
            }
            for ep in sorted(self._endpoints.values(), key=lambda e: e.ema_latency_ms)
        ]
```

---

## Solution 2: Availability-Zone Affinity Router

Prefer same-AZ endpoints first, then same-region, then remote. Cross-AZ traffic costs money and adds latency; keeping traffic within an AZ minimises both.

```python
import os
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class AZEndpoint:
    url: str
    region: str
    az: str
    weight: float = 1.0
    healthy: bool = True


class AZAffinityRouter:
    """
    Routes to same-AZ > same-region > remote, in that priority order.
    Falls back to the next tier when the preferred tier is fully unhealthy.

    Usage:
        router = AZAffinityRouter(
            local_region=os.environ.get("AWS_REGION", "us-east-1"),
            local_az=os.environ.get("AWS_AZ", "us-east-1a"),
            endpoints=[...],
        )
        ep = router.pick()
        await call(ep.url)
    """

    def __init__(self, local_region: str, local_az: str,
                 endpoints: List[AZEndpoint]):
        self._local_region = local_region
        self._local_az = local_az
        self._endpoints = endpoints

    def _healthy(self, eps: List[AZEndpoint]) -> List[AZEndpoint]:
        return [e for e in eps if e.healthy]

    def pick(self) -> Optional[AZEndpoint]:
        # Tier 1: same AZ
        same_az = self._healthy([
            e for e in self._endpoints
            if e.az == self._local_az and e.region == self._local_region
        ])
        if same_az:
            return self._weighted_pick(same_az)

        # Tier 2: same region, different AZ
        same_region = self._healthy([
            e for e in self._endpoints
            if e.region == self._local_region and e.az != self._local_az
        ])
        if same_region:
            return self._weighted_pick(same_region)

        # Tier 3: remote region
        remote = self._healthy([
            e for e in self._endpoints if e.region != self._local_region
        ])
        if remote:
            return self._weighted_pick(remote)

        return None  # no healthy endpoints

    def _weighted_pick(self, candidates: List[AZEndpoint]) -> AZEndpoint:
        import random
        total = sum(e.weight for e in candidates)
        r = random.uniform(0, total)
        cumulative = 0.0
        for ep in candidates:
            cumulative += ep.weight
            if r < cumulative:
                return ep
        return candidates[-1]

    def mark_unhealthy(self, url: str):
        for ep in self._endpoints:
            if ep.url == url:
                ep.healthy = False

    def mark_healthy(self, url: str):
        for ep in self._endpoints:
            if ep.url == url:
                ep.healthy = True
```

---

## Solution 3: Power-of-Two-Choices Load Balancer

Pick two random endpoints and route to the less-loaded one. This achieves near-optimal load distribution with only O(1) state per endpoint and zero coordination.

```python
import random
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class LoadedEndpoint:
    url: str
    region: str
    in_flight: int = 0
    total_requests: int = 0
    last_latency_ms: float = 0.0
    healthy: bool = True

    @property
    def score(self) -> float:
        """Lower is better: in-flight count + latency bias."""
        return self.in_flight + self.last_latency_ms / 1000


class PowerOfTwoChoicesRouter:
    """
    Picks 2 random healthy endpoints and routes to the less-loaded one.
    Achieves O(log log n) max load with O(1) coordination.

    Usage:
        router = PowerOfTwoChoicesRouter(endpoints)
        ep = router.pick()
        try:
            result = await call(ep.url)
            router.complete(ep.url, latency_ms=45.0, success=True)
        except Exception:
            router.complete(ep.url, latency_ms=5000.0, success=False)
    """

    def __init__(self, endpoints: List[LoadedEndpoint]):
        self._endpoints = {ep.url: ep for ep in endpoints}

    def pick(self) -> Optional[LoadedEndpoint]:
        healthy = [e for e in self._endpoints.values() if e.healthy]
        if not healthy:
            return None
        if len(healthy) == 1:
            return healthy[0]
        a, b = random.sample(healthy, 2)
        chosen = a if a.score <= b.score else b
        chosen.in_flight += 1
        chosen.total_requests += 1
        return chosen

    def complete(self, url: str, latency_ms: float, success: bool):
        ep = self._endpoints.get(url)
        if ep is None:
            return
        ep.in_flight = max(0, ep.in_flight - 1)
        ep.last_latency_ms = latency_ms
        if not success and latency_ms > 3000:
            ep.healthy = False

    def utilisation(self) -> dict:
        return {
            url: {"in_flight": ep.in_flight, "total": ep.total_requests}
            for url, ep in self._endpoints.items()
        }
```

---

## Solution 4: Consistent Hash Ring for Stateful Tool Endpoints

When tool calls carry a session or user ID and the backend is stateful (e.g. a vector DB with warm caches), use consistent hashing to route the same user's requests to the same endpoint.

```python
import hashlib
from bisect import bisect_right, insort
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple


@dataclass
class HashRingNode:
    url: str
    region: str
    weight: int = 100   # number of virtual nodes


class ConsistentHashRouter:
    """
    Routes requests by key (user_id, session_id) to the same endpoint.
    When a node is removed, only its fraction of traffic is redistributed.

    Usage:
        router = ConsistentHashRouter([
            HashRingNode("https://db-1.svc", "us-east-1", weight=100),
            HashRingNode("https://db-2.svc", "us-east-1", weight=100),
        ])
        ep = router.pick(key=user_id)
        await call(ep.url)
    """

    def __init__(self, nodes: List[HashRingNode]):
        self._ring: List[Tuple[int, str]] = []   # (hash, url)
        self._nodes: Dict[str, HashRingNode] = {}
        for node in nodes:
            self.add_node(node)

    def _hash(self, key: str) -> int:
        return int(hashlib.md5(key.encode()).hexdigest(), 16)

    def add_node(self, node: HashRingNode):
        self._nodes[node.url] = node
        for i in range(node.weight):
            vnode_key = f"{node.url}#{i}"
            h = self._hash(vnode_key)
            insort(self._ring, (h, node.url))

    def remove_node(self, url: str):
        node = self._nodes.pop(url, None)
        if node is None:
            return
        self._ring = [(h, u) for h, u in self._ring if u != url]

    def pick(self, key: str) -> Optional[HashRingNode]:
        if not self._ring:
            return None
        h = self._hash(key)
        idx = bisect_right(self._ring, (h, "")) % len(self._ring)
        url = self._ring[idx][1]
        return self._nodes.get(url)

    def distribution(self) -> Dict[str, float]:
        """Approximate traffic fraction per node."""
        counts = {url: 0 for url in self._nodes}
        for _, url in self._ring:
            counts[url] += 1
        total = max(1, len(self._ring))
        return {url: round(c / total, 3) for url, c in counts.items()}
```

---

## Solution 5: Geo-Proximity Endpoint Resolver

Map agent deployments to geographic coordinates and pick the endpoint with the smallest great-circle distance to the requesting agent.

```python
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class GeoEndpoint:
    url: str
    name: str
    lat: float
    lon: float
    healthy: bool = True

    def distance_km(self, lat: float, lon: float) -> float:
        """Haversine formula."""
        R = 6371.0
        dlat = math.radians(lat - self.lat)
        dlon = math.radians(lon - self.lon)
        a = (math.sin(dlat / 2) ** 2 +
             math.cos(math.radians(self.lat)) *
             math.cos(math.radians(lat)) *
             math.sin(dlon / 2) ** 2)
        return 2 * R * math.asin(math.sqrt(a))


# Approximate AWS region coordinates
AWS_REGION_COORDS: dict = {
    "us-east-1":      (38.9, -77.0),
    "us-east-2":      (40.4, -82.9),
    "us-west-1":      (37.7, -122.4),
    "us-west-2":      (45.5, -122.7),
    "eu-west-1":      (53.3, -6.3),
    "eu-central-1":   (50.1, 8.7),
    "ap-southeast-1": (1.4, 103.8),
    "ap-northeast-1": (35.7, 139.7),
}


class GeoProximityRouter:
    """
    Routes requests to the geographically closest healthy endpoint.

    Usage:
        router = GeoProximityRouter([
            GeoEndpoint("https://api-ue1.svc", "us-east-1",
                        *AWS_REGION_COORDS["us-east-1"]),
            GeoEndpoint("https://api-ew1.svc", "eu-west-1",
                        *AWS_REGION_COORDS["eu-west-1"]),
        ])
        ep = router.pick(agent_lat=48.9, agent_lon=2.4)   # Paris agent
        await call(ep.url)   # routes to eu-west-1
    """

    def __init__(self, endpoints: List[GeoEndpoint]):
        self._endpoints = endpoints

    def pick(self, agent_lat: float, agent_lon: float,
             agent_region: Optional[str] = None) -> Optional[GeoEndpoint]:
        candidates = [e for e in self._endpoints if e.healthy]
        if not candidates:
            return None
        return min(candidates, key=lambda e: e.distance_km(agent_lat, agent_lon))

    def pick_by_region(self, region: str) -> Optional[GeoEndpoint]:
        lat, lon = AWS_REGION_COORDS.get(region, (0.0, 0.0))
        return self.pick(lat, lon)
```

---

## Solution 6: Composite Topology-Aware Agent Tool Router

Combines AZ affinity, latency-based selection, and health checking into one router with automatic fallback tiers.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ToolEndpoint:
    url: str
    region: str
    az: str
    latency_ms: float = 50.0
    in_flight: int = 0
    healthy: bool = True
    consecutive_errors: int = 0


class CompositeTopologyRouter:
    """
    Unified topology-aware router for agent tool calls.
    Tier selection: same-AZ > same-region > remote, with power-of-two within each tier.

    Usage:
        router = CompositeTopologyRouter(
            local_region="us-east-1", local_az="us-east-1a",
            endpoints=[...],
        )
        asyncio.create_task(router.health_probe_loop(http_client))

        ep = router.pick()
        t0 = time.monotonic()
        try:
            result = await call_tool(ep.url)
            router.record(ep.url, (time.monotonic()-t0)*1000, success=True)
        except Exception:
            router.record(ep.url, 5000.0, success=False)
    """

    def __init__(self, local_region: str, local_az: str,
                 endpoints: List[ToolEndpoint]):
        self._local_region = local_region
        self._local_az = local_az
        self._eps: Dict[str, ToolEndpoint] = {e.url: e for e in endpoints}

    def _tier(self, ep: ToolEndpoint) -> int:
        if ep.az == self._local_az and ep.region == self._local_region:
            return 0
        if ep.region == self._local_region:
            return 1
        return 2

    def _score(self, ep: ToolEndpoint) -> float:
        return ep.latency_ms * (1 + ep.in_flight * 0.1)

    def pick(self) -> Optional[ToolEndpoint]:
        import random
        healthy = [e for e in self._eps.values() if e.healthy]
        if not healthy:
            return None
        for tier in range(3):
            tier_eps = [e for e in healthy if self._tier(e) == tier]
            if len(tier_eps) >= 2:
                a, b = random.sample(tier_eps, 2)
                chosen = a if self._score(a) <= self._score(b) else b
                chosen.in_flight += 1
                return chosen
            elif tier_eps:
                tier_eps[0].in_flight += 1
                return tier_eps[0]
        return None

    def record(self, url: str, latency_ms: float, success: bool):
        ep = self._eps.get(url)
        if ep is None:
            return
        ep.in_flight = max(0, ep.in_flight - 1)
        ep.latency_ms = 0.1 * latency_ms + 0.9 * ep.latency_ms
        if success:
            ep.consecutive_errors = 0
            ep.healthy = True
        else:
            ep.consecutive_errors += 1
            if ep.consecutive_errors >= 3:
                ep.healthy = False

    async def health_probe_loop(self, http_client, interval: float = 15.0):
        while True:
            await asyncio.sleep(interval)
            for ep in list(self._eps.values()):
                t0 = time.monotonic()
                try:
                    await http_client.get(ep.url + "/health", timeout=2.0)
                    ms = (time.monotonic() - t0) * 1000
                    self.record(ep.url, ms, success=True)
                except Exception:
                    self.record(ep.url, 5000.0, success=False)

    def stats(self) -> List[dict]:
        return [
            {
                "url": ep.url, "tier": self._tier(ep),
                "latency_ms": round(ep.latency_ms, 1),
                "healthy": ep.healthy, "in_flight": ep.in_flight,
            }
            for ep in sorted(self._eps.values(), key=lambda e: (self._tier(e), e.latency_ms))
        ]
```

---

## Comparison

| Approach | Latency Benefit | Failure Isolation | State Required |
|---|---|---|---|
| **Latency-Based EMA** | High | Via unhealthy penalty | EMA per endpoint |
| **AZ Affinity** | High (same-AZ) | AZ and region level | Topology labels |
| **Power-of-Two Choices** | Medium (load) | Via in-flight count | In-flight count |
| **Consistent Hash** | Medium (cache hit) | None | Ring structure |
| **Geo-Proximity** | High (cross-region) | None | Lat/lon coords |
| **Composite Router** | Highest | AZ + region + health | All of above |

**Key insight**: deploy the AZ affinity router first — it's the highest-impact change with minimal complexity. Add latency EMA updates to handle cases where the nearest AZ is overloaded. Use consistent hashing only for stateful backends where cache locality matters.
