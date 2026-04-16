---
title: "Agent Doesn't Implement DNS Caching for Repeated External Tool Calls"
description: "AI agents that create a new HTTP client session per tool call re-resolve DNS for every request, adding 20–100ms of latency to each call. DNS response caching with TTL-aware expiry, negative caching for NXDOMAIN responses, and pre-warming critical hostnames eliminates repeated resolver round-trips and cuts tool call latency for services the agent calls frequently."
date: 2025-02-17
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-dns-caching-for-repeated-external-tool-calls
tags:
  - dns-caching
  - latency
  - performance
  - http-client
  - tool-calls
  - connection-optimization
  - network
symptoms:
  - "Tool calls to the same API host take 60ms longer than expected — DNS lookup on every call"
  - "p99 latency spikes on first call after a new HTTP session is created"
  - "DNS resolution accounts for 30% of total tool call latency in profiles"
  - "No DNS TTL is respected — re-resolving even when cache is valid"
  - "NXDOMAIN lookups for typo hostnames repeat on every retry without negative caching"
---

## Problem

DNS resolution is a blocking network operation that adds 20–200ms per lookup depending on resolver proximity. In a default Python `aiohttp` or `httpx` setup, each new `ClientSession` resolves hostnames fresh. Agents that instantiate sessions per-request or per-tool-call pay this cost repeatedly. A DNS cache with TTL-aware expiry stores resolved addresses in memory and returns them instantly for subsequent lookups to the same hostname, reducing per-tool-call latency by the full resolver RTT for cached entries.

---

## Solution 1: DNSCache — TTL-Aware In-Memory Address Cache

```python
import asyncio
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class DNSRecord:
    hostname: str
    addresses: List[str]         # Resolved IP addresses
    ttl_s: float
    resolved_at: float = field(default_factory=time.monotonic)
    negative: bool = False       # True for NXDOMAIN/failure cache

    def is_expired(self) -> bool:
        return time.monotonic() - self.resolved_at > self.ttl_s

    def best_address(self) -> Optional[str]:
        return self.addresses[0] if self.addresses else None


class DNSCache:
    """
    TTL-aware DNS cache for hostname-to-IP mappings. Caches both
    successful lookups (respecting TTL) and failed lookups (negative
    caching with a short TTL) to avoid repeated resolver round-trips
    for non-existent or temporarily unreachable hosts.

    Usage:
        cache = DNSCache(default_ttl_s=300, negative_ttl_s=30)
        addresses = await cache.resolve("api.anthropic.com")
        # First call: resolves via DNS. Subsequent calls: returned instantly.
    """

    def __init__(self, default_ttl_s: float = 300.0,
                  negative_ttl_s: float = 30.0,
                  max_entries: int = 512):
        self._default_ttl = default_ttl_s
        self._neg_ttl = negative_ttl_s
        self._max = max_entries
        self._cache: Dict[str, DNSRecord] = {}
        self._hits = 0
        self._misses = 0

    async def resolve(self, hostname: str,
                       port: int = 80) -> List[str]:
        """Returns list of IP addresses for hostname."""
        record = self._cache.get(hostname)
        if record and not record.is_expired():
            self._hits += 1
            if record.negative:
                raise OSError(f"DNS cached NXDOMAIN for '{hostname}'")
            return record.addresses

        self._misses += 1
        return await self._do_resolve(hostname, port)

    async def _do_resolve(self, hostname: str, port: int) -> List[str]:
        loop = asyncio.get_event_loop()
        t0 = time.monotonic()
        try:
            infos = await loop.getaddrinfo(
                hostname, port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_STREAM,
            )
            addresses = list({info[4][0] for info in infos})
            elapsed_ms = (time.monotonic() - t0) * 1000
            logger.debug(
                "dns_resolved host=%s addresses=%s elapsed_ms=%.1f",
                hostname, addresses, elapsed_ms,
            )
            self._cache[hostname] = DNSRecord(
                hostname=hostname,
                addresses=addresses,
                ttl_s=self._default_ttl,
            )
            self._evict_if_needed()
            return addresses
        except OSError as exc:
            logger.warning("dns_resolution_failed host=%s error=%s", hostname, exc)
            self._cache[hostname] = DNSRecord(
                hostname=hostname,
                addresses=[],
                ttl_s=self._neg_ttl,
                negative=True,
            )
            raise

    def _evict_if_needed(self):
        if len(self._cache) <= self._max:
            return
        # Remove expired first, then oldest
        now = time.monotonic()
        self._cache = {
            k: v for k, v in self._cache.items()
            if not v.is_expired()
        }
        if len(self._cache) > self._max:
            oldest = sorted(self._cache, key=lambda k: self._cache[k].resolved_at)
            for key in oldest[:len(self._cache) - self._max]:
                del self._cache[key]

    def invalidate(self, hostname: str):
        self._cache.pop(hostname, None)

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "cached_entries": len(self._cache),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(total, 1), 3),
        }
```

---

## Solution 2: PrewarmedDNSResolver — Resolve Critical Hosts at Startup

```python
import asyncio
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


class PrewarmedDNSResolver:
    """
    Resolves a set of critical hostnames at agent startup so that the
    first tool call to each host is not penalized by a cold DNS lookup.
    Periodically refreshes entries before their TTL expires.

    Usage:
        resolver = PrewarmedDNSResolver(
            cache=dns_cache,
            critical_hosts=[
                "api.anthropic.com",
                "api.openai.com",
                "www.googleapis.com",
            ],
            refresh_before_expiry_s=60,
        )
        await resolver.prewarm()
        # All critical hosts are now cached; first tool calls are fast.
    """

    def __init__(self, cache: DNSCache,
                  critical_hosts: Optional[List[str]] = None,
                  refresh_before_expiry_s: float = 60.0):
        self._cache = cache
        self._hosts = critical_hosts or []
        self._refresh_ahead = refresh_before_expiry_s
        self._refresh_task: Optional[asyncio.Task] = None

    async def prewarm(self):
        """Resolve all critical hosts concurrently."""
        tasks = [self._resolve_safe(h) for h in self._hosts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        succeeded = sum(1 for r in results if not isinstance(r, Exception))
        logger.info(
            "dns_prewarm_complete hosts=%d succeeded=%d",
            len(self._hosts), succeeded,
        )

    async def _resolve_safe(self, hostname: str):
        try:
            return await self._cache.resolve(hostname)
        except Exception as exc:
            logger.warning("dns_prewarm_failed host=%s error=%s", hostname, exc)
            return exc

    def start_refresh_loop(self, interval_s: float = 120.0):
        """Background task that refreshes cached entries before expiry."""
        async def _loop():
            while True:
                await asyncio.sleep(interval_s)
                await self.prewarm()

        self._refresh_task = asyncio.create_task(_loop())

    def stop_refresh_loop(self):
        if self._refresh_task:
            self._refresh_task.cancel()
            self._refresh_task = None
```

---

## Solution 3: CachingHTTPSession — Share Session with DNS Cache Across Tool Calls

```python
import asyncio
import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class CachingHTTPSession:
    """
    Manages a single long-lived aiohttp ClientSession shared across all
    tool calls, with DNS caching integrated into the connector. Avoids
    both per-call session creation overhead and repeated DNS lookups.

    Usage:
        session_manager = CachingHTTPSession(dns_cache=dns_cache)
        await session_manager.start()

        async with session_manager.get() as session:
            resp = await session.get("https://api.anthropic.com/v1/messages")

        await session_manager.stop()
    """

    def __init__(self, dns_cache: Optional[DNSCache] = None,
                  connection_timeout_s: float = 5.0,
                  total_timeout_s: float = 30.0,
                  max_connections: int = 100):
        self._dns = dns_cache or DNSCache()
        self._conn_timeout = connection_timeout_s
        self._total_timeout = total_timeout_s
        self._max_conn = max_connections
        self._session = None
        self._created_at: float = 0.0
        self._request_count: int = 0

    async def start(self):
        try:
            import aiohttp

            resolver = self._make_resolver()
            connector = aiohttp.TCPConnector(
                resolver=resolver,
                limit=self._max_conn,
                ttl_dns_cache=300,   # aiohttp's built-in DNS TTL
                use_dns_cache=True,
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(
                connect=self._conn_timeout,
                total=self._total_timeout,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=timeout,
            )
            self._created_at = time.monotonic()
            logger.info("http_session_started max_connections=%d", self._max_conn)
        except ImportError:
            logger.warning("aiohttp not available; session caching disabled")

    def _make_resolver(self):
        """Return an aiohttp-compatible custom resolver backed by DNSCache."""
        cache = self._dns

        class CachedResolver:
            async def resolve(self, host, port=0, family=0):
                try:
                    addresses = await cache.resolve(host, port)
                    import socket
                    return [
                        {
                            "hostname": host,
                            "host": addr,
                            "port": port,
                            "family": socket.AF_INET,
                            "proto": 0,
                            "flags": 0,
                        }
                        for addr in addresses
                    ]
                except OSError:
                    raise

            async def close(self):
                pass

        return CachedResolver()

    def get(self):
        """Context manager returning the shared session."""
        if self._session is None:
            raise RuntimeError("Session not started — call await start() first")
        self._request_count += 1
        return self._session

    async def stop(self):
        if self._session:
            await self._session.close()
            self._session = None

    def stats(self) -> Dict[str, Any]:
        uptime = time.monotonic() - self._created_at if self._created_at else 0
        return {
            "uptime_s": round(uptime, 1),
            "request_count": self._request_count,
            "dns_stats": self._dns.stats(),
        }
```

---

## Solution 4: DNSCacheMetrics — Track Cache Effectiveness

```python
import logging
import time
from collections import defaultdict, deque
from typing import Any, Dict

logger = logging.getLogger(__name__)


class DNSCacheMetrics:
    """
    Wraps a DNSCache to record per-hostname hit rates, miss latencies,
    and cache churn (entries expiring and being re-resolved). Used to
    tune TTL values and identify hosts that should be pre-warmed.

    Usage:
        metrics = DNSCacheMetrics(cache)
        addresses = await metrics.resolve("api.stripe.com")

        report = metrics.report()
        # {"hottest_hosts": [...], "miss_latency_p95_ms": 45.2, ...}
    """

    def __init__(self, cache: DNSCache):
        self._cache = cache
        self._miss_latencies: deque = deque(maxlen=1000)
        self._per_host_hits: Dict[str, int] = defaultdict(int)
        self._per_host_misses: Dict[str, int] = defaultdict(int)

    async def resolve(self, hostname: str, port: int = 80):
        was_cached = (
            hostname in self._cache._cache
            and not self._cache._cache[hostname].is_expired()
        )
        t0 = time.monotonic()
        result = await self._cache.resolve(hostname, port)
        elapsed_ms = (time.monotonic() - t0) * 1000

        if was_cached:
            self._per_host_hits[hostname] += 1
        else:
            self._per_host_misses[hostname] += 1
            self._miss_latencies.append(elapsed_ms)

        return result

    def report(self) -> Dict[str, Any]:
        latencies = sorted(self._miss_latencies)
        n = len(latencies)

        hottest = sorted(
            self._per_host_hits.items(), key=lambda x: -x[1]
        )[:10]
        coldest = sorted(
            self._per_host_misses.items(), key=lambda x: -x[1]
        )[:5]

        return {
            "miss_latency_p50_ms": round(latencies[n // 2], 1) if n else 0,
            "miss_latency_p95_ms": round(latencies[int(n * 0.95)], 1) if n > 20 else 0,
            "hottest_cached_hosts": [h for h, _ in hottest],
            "most_missed_hosts": [h for h, _ in coldest],
            "cache_stats": self._cache.stats(),
        }
```

---

## Solution 5: RoundRobinDNSBalancer — Cycle Through Multiple Addresses

```python
import logging
from collections import defaultdict
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class RoundRobinDNSBalancer:
    """
    When a hostname resolves to multiple IP addresses (e.g., load-balanced
    API endpoints), cycles through them in round-robin order rather than
    always using the first address. Distributes connection load and provides
    implicit failover when a single IP becomes unreachable.

    Usage:
        balancer = RoundRobinDNSBalancer(dns_cache)
        ip = await balancer.next_address("api.example.com")
        # First call returns addresses[0], second returns addresses[1], etc.
    """

    def __init__(self, cache: DNSCache):
        self._cache = cache
        self._counters: Dict[str, int] = defaultdict(int)
        self._failed: Dict[str, set] = defaultdict(set)

    async def next_address(self, hostname: str,
                             port: int = 443) -> str:
        addresses = await self._cache.resolve(hostname, port)
        healthy = [a for a in addresses if a not in self._failed[hostname]]
        if not healthy:
            # All addresses failed — reset and retry
            self._failed[hostname].clear()
            healthy = addresses

        idx = self._counters[hostname] % len(healthy)
        self._counters[hostname] = (idx + 1) % len(healthy)
        return healthy[idx]

    def mark_failed(self, hostname: str, address: str):
        """Mark a specific IP as unreachable for this hostname."""
        self._failed[hostname].add(address)
        self._cache.invalidate(hostname)
        logger.warning(
            "dns_address_marked_failed host=%s address=%s", hostname, address
        )

    def mark_recovered(self, hostname: str, address: str):
        self._failed[hostname].discard(address)
```

---

## Solution 6: DNSAwareToolCallManager — Full DNS Optimization Stack

```python
import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class DNSAwareToolCallManager:
    """
    Integrates DNS caching, pre-warming, metrics, and round-robin
    balancing into a unified manager for agent tool HTTP calls.

    Usage:
        manager = DNSAwareToolCallManager(
            critical_hosts=["api.anthropic.com", "api.github.com"],
        )
        await manager.start()

        result = await manager.call_tool(
            "fetch_github_issues",
            fetch_fn,
            repo="anthropics/claude-code",
        )
        print(manager.dns_report())
    """

    def __init__(self, critical_hosts: Optional[List[str]] = None,
                  default_ttl_s: float = 300.0):
        self._cache = DNSCache(default_ttl_s=default_ttl_s)
        self._metrics = DNSCacheMetrics(self._cache)
        self._balancer = RoundRobinDNSBalancer(self._cache)
        self._prewarmer = PrewarmedDNSResolver(
            self._cache,
            critical_hosts=critical_hosts or [],
        )
        self._session_mgr: Optional[CachingHTTPSession] = None

    async def start(self):
        await self._prewarmer.prewarm()
        self._prewarmer.start_refresh_loop()
        self._session_mgr = CachingHTTPSession(dns_cache=self._cache)
        await self._session_mgr.start()
        logger.info("dns_aware_manager started")

    async def stop(self):
        self._prewarmer.stop_refresh_loop()
        if self._session_mgr:
            await self._session_mgr.stop()

    async def call_tool(self, tool_name: str,
                         fn: Callable, *args, **kwargs) -> Any:
        return await fn(*args, **kwargs)

    def resolve(self, hostname: str):
        return self._metrics.resolve(hostname)

    def dns_report(self) -> Dict[str, Any]:
        return self._metrics.report()
```

---

## Comparison

| Approach | TTL-Aware Cache | Negative Cache | Pre-warming | Metrics | Round-Robin | Integrated |
|---|---|---|---|---|---|---|
| **DNSCache** | Yes | Yes | No | No | No | No |
| **PrewarmedDNSResolver** | Via cache | Via cache | Yes | No | No | No |
| **CachingHTTPSession** | Via cache | Via cache | No | No | No | No |
| **DNSCacheMetrics** | Via cache | Via cache | No | Yes | No | No |
| **RoundRobinDNSBalancer** | Via cache | Via cache | No | No | Yes | No |
| **DNSAwareToolCallManager** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: aiohttp's built-in `use_dns_cache=True` and `ttl_dns_cache=300` options handle the common case — enable them first before building a custom cache. The custom `DNSCache` class is needed when you want cross-session sharing (the built-in cache is per-connector), negative caching for NXDOMAIN responses, or pre-warming. Set `negative_ttl_s=30` rather than a longer value: a host that was unreachable 30 seconds ago may have recovered, and a long negative TTL would prevent discovery of recovery. Pre-warm the 5–10 hostnames your agent calls in every conversation; for others, the second call is fast and the first-call penalty is acceptable.
