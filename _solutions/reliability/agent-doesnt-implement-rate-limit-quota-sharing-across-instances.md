---
title: "Agent Doesn't Implement Rate Limit Quota Sharing Across Instances"
description: "Agents deployed as multiple instances that manage rate limit quotas locally each believe they have the full quota, causing collective over-consumption that triggers 429 errors: three instances each allowing 100 requests/minute against a 100 req/min shared API limit results in 300 req/min actual demand. Implement distributed quota sharing using a central token bucket in Redis so all instances collectively respect the true API rate limit."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-rate-limit-quota-sharing-across-instances
tags: [rate-limiting, quota-sharing, distributed-systems, token-bucket, redis, multi-instance]
symptoms:
  - "429 errors appear even though each individual instance stays within its local rate limit"
  - "Rate limit errors increase linearly with the number of deployed instances"
  - "No shared state between instances for tracking consumed quota"
  - "Each instance initializes a fresh token bucket on startup with the full limit"
  - "Horizontal scaling makes API overuse worse, not better"
---

## Why This Happens

A rate limit enforced per-process does not account for sibling processes making requests to the same API under the same credentials. If the API allows 100 requests per minute and three instances each enforce a 100 req/min local bucket, the net request rate is 300 req/min — three times the actual quota. Distributed quota sharing requires a central authority that all instances consult before making a request. Redis is the standard choice: atomic Lua scripts or the `INCRBY` + `EXPIRE` pattern implement a shared token bucket without race conditions. Each instance deducts from the shared pool and backs off when the pool is empty.

## Solution 1: Shared Quota Key Schema

```python
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class QuotaKey:
    """Identifies a rate limit bucket in the shared store."""
    api_name: str
    credential_id: str       # hash of the API key, not the key itself
    window_seconds: int = 60

    @property
    def redis_key(self) -> str:
        window_start = int(time.time()) // self.window_seconds
        return f"quota:{self.api_name}:{self.credential_id}:{window_start}"

    @property
    def ttl_seconds(self) -> int:
        return self.window_seconds * 2   # survive one full window overlap


@dataclass
class QuotaReservation:
    granted: bool
    tokens_granted: int
    tokens_remaining: int
    retry_after_seconds: float
    instance_id: str
    quota_key: str
```

## Solution 2: Redis Shared Token Bucket

```python
import time
from typing import Optional


ACQUIRE_SCRIPT = """
local key = KEYS[1]
local limit = tonumber(ARGV[1])
local requested = tonumber(ARGV[2])
local ttl = tonumber(ARGV[3])

local current = tonumber(redis.call('GET', key) or '0')
local available = limit - current

if available >= requested then
    redis.call('INCRBY', key, requested)
    redis.call('EXPIRE', key, ttl)
    return {1, available - requested}
else
    return {0, available}
end
"""


class RedisSharedTokenBucket:
    """
    Distributed token bucket backed by Redis. All instances share a
    single quota key per (api, credential, time window) tuple.
    Atomic Lua script prevents over-consumption under concurrent access.
    """

    def __init__(self, redis_client, limit_per_window: int, instance_id: str = ""):
        self._redis = redis_client
        self._limit = limit_per_window
        self._instance_id = instance_id
        self._script = redis_client.register_script(ACQUIRE_SCRIPT)

    def acquire(self, quota_key: QuotaKey, tokens: int = 1) -> QuotaReservation:
        key = quota_key.redis_key
        result = self._script(keys=[key], args=[self._limit, tokens, quota_key.ttl_seconds])
        granted_flag, remaining = int(result[0]), int(result[1])

        retry_after = 0.0
        if not granted_flag:
            # Estimate when window resets
            window_end = (int(time.time()) // quota_key.window_seconds + 1) * quota_key.window_seconds
            retry_after = max(0.0, window_end - time.time())

        return QuotaReservation(
            granted=bool(granted_flag),
            tokens_granted=tokens if granted_flag else 0,
            tokens_remaining=remaining,
            retry_after_seconds=retry_after,
            instance_id=self._instance_id,
            quota_key=key,
        )

    def current_usage(self, quota_key: QuotaKey) -> int:
        val = self._redis.get(quota_key.redis_key)
        return int(val) if val else 0
```

## Solution 3: Quota-Aware API Client Wrapper

```python
import asyncio
import time
from typing import Any, Callable, Optional


class QuotaAwareAPIClient:
    """
    Wraps any async API call with shared quota enforcement.
    Blocks until quota is available or raises on timeout.
    """

    def __init__(
        self,
        bucket: RedisSharedTokenBucket,
        quota_key: QuotaKey,
        max_wait_seconds: float = 30.0,
        tokens_per_call: int = 1,
    ):
        self._bucket = bucket
        self._key = quota_key
        self._max_wait = max_wait_seconds
        self._tokens = tokens_per_call
        self._blocked_count = 0
        self._total_calls = 0

    async def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        deadline = time.time() + self._max_wait
        self._total_calls += 1

        while True:
            reservation = self._bucket.acquire(self._key, self._tokens)
            if reservation.granted:
                return await fn(*args, **kwargs)

            self._blocked_count += 1
            wait = min(reservation.retry_after_seconds, deadline - time.time())
            if wait <= 0:
                raise QuotaExhaustedError(
                    api=self._key.api_name,
                    retry_after=reservation.retry_after_seconds,
                )
            await asyncio.sleep(wait)

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "blocked_calls": self._blocked_count,
            "block_rate": round(self._blocked_count / max(self._total_calls, 1), 4),
            "current_usage": self._bucket.current_usage(self._key),
            "limit": self._bucket._limit,
        }


class QuotaExhaustedError(Exception):
    def __init__(self, api: str, retry_after: float):
        super().__init__(f"quota exhausted for '{api}', retry after {retry_after:.1f}s")
        self.api = api
        self.retry_after = retry_after
```

## Solution 4: Multi-API Quota Registry

```python
import hashlib
from typing import Dict, Optional


class SharedQuotaRegistry:
    """
    Manages shared quota buckets for multiple APIs and credentials.
    Buckets are created lazily on first access.
    """

    def __init__(
        self,
        redis_client,
        instance_id: str,
        default_limit_per_window: int = 100,
        default_window_seconds: int = 60,
    ):
        self._redis = redis_client
        self._instance_id = instance_id
        self._default_limit = default_limit_per_window
        self._default_window = default_window_seconds
        self._buckets: Dict[str, RedisSharedTokenBucket] = {}
        self._limits: Dict[str, int] = {}

    def _credential_id(self, api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()[:16]

    def register(
        self,
        api_name: str,
        api_key: str,
        limit_per_window: int,
        window_seconds: int = 60,
    ) -> None:
        bucket_key = f"{api_name}:{self._credential_id(api_key)}"
        self._limits[bucket_key] = limit_per_window
        self._buckets[bucket_key] = RedisSharedTokenBucket(
            redis_client=self._redis,
            limit_per_window=limit_per_window,
            instance_id=self._instance_id,
        )

    def get_client(
        self,
        api_name: str,
        api_key: str,
        tokens_per_call: int = 1,
        max_wait_seconds: float = 30.0,
    ) -> QuotaAwareAPIClient:
        cred_id = self._credential_id(api_key)
        bucket_key = f"{api_name}:{cred_id}"

        if bucket_key not in self._buckets:
            self._buckets[bucket_key] = RedisSharedTokenBucket(
                redis_client=self._redis,
                limit_per_window=self._default_limit,
                instance_id=self._instance_id,
            )

        quota_key = QuotaKey(
            api_name=api_name,
            credential_id=cred_id,
            window_seconds=self._default_window,
        )
        return QuotaAwareAPIClient(
            bucket=self._buckets[bucket_key],
            quota_key=quota_key,
            max_wait_seconds=max_wait_seconds,
            tokens_per_call=tokens_per_call,
        )

    def all_usage(self) -> dict:
        result = {}
        for bucket_key, bucket in self._buckets.items():
            api_name, cred_id = bucket_key.rsplit(":", 1)
            quota_key = QuotaKey(api_name=api_name, credential_id=cred_id)
            result[bucket_key] = {
                "usage": bucket.current_usage(quota_key),
                "limit": self._limits.get(bucket_key, self._default_limit),
            }
        return result
```

## Solution 5: Quota Burst Smoothing Scheduler

```python
import asyncio
import time
from typing import Any, Callable, List


class QuotaBurstSmoothingScheduler:
    """
    When quota is scarce, smooths request dispatch across the remaining
    window rather than allowing all instances to race at window reset.
    Adds randomized jitter so instances do not synchronize exactly.
    """

    def __init__(
        self,
        quota_key: QuotaKey,
        bucket: RedisSharedTokenBucket,
        max_burst_fraction: float = 0.3,  # use at most 30% of quota in first 10% of window
    ):
        self._key = quota_key
        self._bucket = bucket
        self._max_burst = max_burst_fraction

    def _smoothed_delay(self, tokens_remaining: int) -> float:
        """
        Compute a delay that spreads remaining requests across the window.
        """
        import random
        window = self._key.window_seconds
        elapsed = time.time() % window
        time_left = window - elapsed
        if tokens_remaining <= 0 or time_left <= 0:
            return 0.0
        rate = tokens_remaining / time_left   # tokens per second
        base_delay = 1.0 / max(rate, 0.01)
        jitter = random.uniform(0, base_delay * 0.2)
        return base_delay + jitter

    async def schedule(
        self,
        fn: Callable,
        *args: Any,
        smooth_threshold: int = 10,  # start smoothing when fewer than N tokens remain
        **kwargs: Any,
    ) -> Any:
        reservation = self._bucket.acquire(self._key)
        if not reservation.granted:
            raise QuotaExhaustedError(api=self._key.api_name, retry_after=reservation.retry_after_seconds)

        if reservation.tokens_remaining < smooth_threshold:
            delay = self._smoothed_delay(reservation.tokens_remaining)
            if delay > 0:
                await asyncio.sleep(delay)

        return await fn(*args, **kwargs)
```

## Solution 6: Quota Sharing Dashboard

```python
import time
from typing import List


class QuotaSharingDashboard:
    """
    Aggregates shared quota usage across all registered APIs and provides
    per-window utilization rates for capacity planning.
    """

    def __init__(
        self,
        registry: SharedQuotaRegistry,
        instance_id: str,
    ):
        self._registry = registry
        self._instance_id = instance_id

    def render(self) -> dict:
        all_usage = self._registry.all_usage()
        api_summaries = []
        for bucket_key, data in all_usage.items():
            usage = data["usage"]
            limit = data["limit"]
            utilization = usage / max(limit, 1)
            api_summaries.append({
                "bucket": bucket_key,
                "usage": usage,
                "limit": limit,
                "utilization_pct": round(utilization * 100, 1),
                "status": (
                    "critical" if utilization >= 0.9
                    else "warning" if utilization >= 0.7
                    else "ok"
                ),
            })

        return {
            "generated_at": time.time(),
            "instance_id": self._instance_id,
            "apis": api_summaries,
            "critical_count": sum(1 for a in api_summaries if a["status"] == "critical"),
            "warning_count": sum(1 for a in api_summaries if a["status"] == "warning"),
        }
```

## Comparison

| Approach | Shared State | Atomic Reserve | Multi-API | Burst Smoothing | Dashboard |
|---|---|---|---|---|---|
| RedisSharedTokenBucket | Yes (Redis) | Yes (Lua) | No | No | No |
| QuotaAwareAPIClient | Via bucket | Via bucket | No | No | No |
| SharedQuotaRegistry | Via buckets | Via buckets | Yes | No | No |
| QuotaBurstSmoothingScheduler | Via bucket | Via bucket | No | Yes | No |
| QuotaSharingDashboard | Via registry | No | Yes | No | Yes |

**Best for production**: Use `RedisSharedTokenBucket` with a Lua script for atomicity — `INCRBY` + `GET` in two separate commands has a race condition where two instances can both read "space available" before either increments. Set the Redis key TTL to `window_seconds * 2` to handle clock skew between instances. Register each credential separately in `SharedQuotaRegistry` so that different API keys for the same service have independent buckets. Monitor `utilization_pct` via `QuotaSharingDashboard`: consistently above 80% means the quota itself needs to be upgraded, not just the smoothing strategy.
