---
title: "Agent Doesn't Implement Rate Limiting Per User for Agent Invocations"
description: "Agents without per-user rate limits allow a single abusive or compromised account to exhaust shared API capacity — sending thousands of requests per minute, triggering LLM cost spikes, and degrading service for all other users. Implement per-user rate limiting that enforces request rate and token consumption limits per authenticated identity using a token bucket or sliding window algorithm, with graduated responses that throttle before hard-blocking."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-rate-limiting-per-user-for-agent-invocations
tags: [rate-limiting, per-user, token-bucket, abuse-prevention, api-quota, throttling]
symptoms:
  - "Single user account sends thousands of requests per minute — no enforcement"
  - "API cost spikes traced to one abusive session with no automatic throttling"
  - "All users share a global rate limit — one bad actor degrades everyone"
  - "No distinction between legitimate high-volume users and abusive patterns"
  - "Rate limits exist at the infrastructure layer but not at the agent application layer"
---

## Why This Happens

Infrastructure-level rate limits (load balancer, API gateway) are coarse-grained and shared across all users. Application-layer per-user rate limiting requires associating each request with an authenticated user identity and enforcing a limit against that identity's counter. Without this, the agent treats all requests from a single server-level connection identically regardless of which user originated them. Token bucket and sliding window algorithms provide smooth limiting that allows legitimate burst traffic while preventing sustained abuse — superior to hard per-minute counters that penalize legitimate bursts.

## Solution 1: Rate Limit Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class RateLimitAction(str, Enum):
    ALLOW = "allow"
    THROTTLE = "throttle"   # add delay
    REJECT = "reject"       # return 429


@dataclass
class UserRateLimitPolicy:
    requests_per_minute: int = 60
    requests_per_hour: int = 1000
    tokens_per_hour: int = 500_000        # LLM tokens consumed
    burst_allowance: int = 20             # extra requests allowed in burst
    throttle_threshold_pct: float = 80.0  # throttle when 80% of limit used
    tier: str = "standard"               # "free" | "standard" | "premium"


DEFAULT_POLICIES: Dict[str, UserRateLimitPolicy] = {
    "free": UserRateLimitPolicy(
        requests_per_minute=10, requests_per_hour=100,
        tokens_per_hour=50_000, burst_allowance=5, tier="free",
    ),
    "standard": UserRateLimitPolicy(
        requests_per_minute=60, requests_per_hour=1000,
        tokens_per_hour=500_000, burst_allowance=20, tier="standard",
    ),
    "premium": UserRateLimitPolicy(
        requests_per_minute=300, requests_per_hour=10_000,
        tokens_per_hour=5_000_000, burst_allowance=100, tier="premium",
    ),
}
```

## Solution 2: Token Bucket Rate Limiter

```python
import time
from dataclasses import dataclass, field
from threading import Lock


@dataclass
class TokenBucketState:
    capacity: float                          # max tokens in bucket
    tokens: float                            # current tokens
    refill_rate: float                       # tokens per second
    last_refill: float = field(default_factory=time.time)
    total_consumed: int = 0
    total_rejected: int = 0


class TokenBucketRateLimiter:
    """
    Token bucket algorithm: bucket fills at a fixed rate,
    each request consumes one token. Allows burst up to capacity.
    Thread-safe via per-bucket lock.
    """

    def __init__(self, capacity: float, refill_rate_per_second: float):
        self._state = TokenBucketState(
            capacity=capacity,
            tokens=capacity,
            refill_rate=refill_rate_per_second,
        )
        self._lock = Lock()

    def try_consume(self, cost: float = 1.0) -> bool:
        with self._lock:
            now = time.time()
            elapsed = now - self._state.last_refill
            self._state.tokens = min(
                self._state.capacity,
                self._state.tokens + elapsed * self._state.refill_rate,
            )
            self._state.last_refill = now

            if self._state.tokens >= cost:
                self._state.tokens -= cost
                self._state.total_consumed += 1
                return True

            self._state.total_rejected += 1
            return False

    def utilization(self) -> float:
        with self._lock:
            return round(1.0 - self._state.tokens / max(self._state.capacity, 1), 4)

    def stats(self) -> dict:
        with self._lock:
            return {
                "tokens_available": round(self._state.tokens, 2),
                "capacity": self._state.capacity,
                "utilization_pct": round(self.utilization() * 100, 2),
                "total_consumed": self._state.total_consumed,
                "total_rejected": self._state.total_rejected,
            }
```

## Solution 3: Per-User Rate Limit Registry

```python
import time
from threading import Lock
from typing import Dict, Optional


class PerUserRateLimitRegistry:
    """
    Maintains per-user token bucket instances, creating them lazily.
    Cleans up buckets for inactive users to bound memory consumption.
    """

    def __init__(
        self,
        default_policy: UserRateLimitPolicy,
        user_ttl_seconds: float = 3600.0,
    ):
        self._default_policy = default_policy
        self._user_ttl = user_ttl_seconds
        self._buckets: Dict[str, TokenBucketRateLimiter] = {}
        self._policies: Dict[str, UserRateLimitPolicy] = {}
        self._last_seen: Dict[str, float] = {}
        self._lock = Lock()

    def set_policy(self, user_id: str, policy: UserRateLimitPolicy) -> None:
        with self._lock:
            self._policies[user_id] = policy
            if user_id in self._buckets:
                del self._buckets[user_id]  # recreate with new policy

    def _get_or_create(self, user_id: str) -> TokenBucketRateLimiter:
        if user_id not in self._buckets:
            policy = self._policies.get(user_id, self._default_policy)
            # Capacity = burst_allowance + requests per minute
            capacity = policy.requests_per_minute + policy.burst_allowance
            refill_rate = policy.requests_per_minute / 60.0
            self._buckets[user_id] = TokenBucketRateLimiter(capacity, refill_rate)
        self._last_seen[user_id] = time.time()
        return self._buckets[user_id]

    def check(self, user_id: str) -> tuple[bool, float]:
        with self._lock:
            self._evict_inactive()
            bucket = self._get_or_create(user_id)
            allowed = bucket.try_consume()
            utilization = bucket.utilization()
        return allowed, utilization

    def _evict_inactive(self) -> None:
        now = time.time()
        inactive = [
            uid for uid, ts in self._last_seen.items()
            if now - ts > self._user_ttl
        ]
        for uid in inactive:
            self._buckets.pop(uid, None)
            self._last_seen.pop(uid, None)

    def user_stats(self, user_id: str) -> Optional[dict]:
        with self._lock:
            bucket = self._buckets.get(user_id)
            return bucket.stats() if bucket else None

    def total_tracked_users(self) -> int:
        with self._lock:
            return len(self._buckets)
```

## Solution 4: Rate Limit Gate

```python
import time
from typing import Optional


class RateLimitGate:
    """
    Applies per-user rate limiting at the agent invocation boundary.
    Returns a RateLimitDecision with the action and retry-after guidance.
    """

    def __init__(
        self,
        registry: PerUserRateLimitRegistry,
        throttle_delay_seconds: float = 2.0,
        audit_logger: Optional["RateLimitAuditLogger"] = None,
    ):
        self._registry = registry
        self._throttle_delay = throttle_delay_seconds
        self._audit = audit_logger

    def check(self, user_id: str) -> dict:
        allowed, utilization = self._registry.check(user_id)

        if not allowed:
            policy = self._registry._policies.get(
                user_id, self._registry._default_policy
            )
            retry_after = 60.0 / policy.requests_per_minute
            decision = {
                "action": RateLimitAction.REJECT.value,
                "user_id": user_id,
                "utilization_pct": round(utilization * 100, 2),
                "retry_after_seconds": round(retry_after, 1),
                "reason": "rate_limit_exceeded",
            }
            if self._audit:
                self._audit.record_rejection(user_id, utilization)
            return decision

        throttle_threshold = (
            self._registry._policies.get(
                user_id, self._registry._default_policy
            ).throttle_threshold_pct / 100.0
        )

        if utilization >= throttle_threshold:
            return {
                "action": RateLimitAction.THROTTLE.value,
                "user_id": user_id,
                "utilization_pct": round(utilization * 100, 2),
                "throttle_delay_seconds": self._throttle_delay,
                "reason": "approaching_rate_limit",
            }

        return {
            "action": RateLimitAction.ALLOW.value,
            "user_id": user_id,
            "utilization_pct": round(utilization * 100, 2),
        }
```

## Solution 5: Rate Limit Audit Logger

```python
import time
from typing import List


class RateLimitAuditLogger:
    """
    Records rate limit rejections and throttle events per user.
    Surfaces abusive patterns and false positives for policy tuning.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: List[dict] = []

    def record_rejection(self, user_id: str, utilization: float) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "user_id": user_id,
            "utilization": utilization,
            "event": "rejected",
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        by_user: dict = {}
        for r in recent:
            by_user[r["user_id"]] = by_user.get(r["user_id"], 0) + 1
        top_users = sorted(by_user.items(), key=lambda x: x[1], reverse=True)[:10]
        return {
            "window_seconds": window_seconds,
            "total_rejections": len(recent),
            "unique_rejected_users": len(by_user),
            "top_rejected_users": top_users,
        }
```

## Solution 6: Per-User Rate Limit Dashboard

```python
import time


class PerUserRateLimitDashboard:
    """
    Combines registry statistics, rejection audit summaries, and
    policy configurations into a single rate limiting health view.
    """

    def __init__(
        self,
        registry: PerUserRateLimitRegistry,
        gate: RateLimitGate,
        audit_logger: RateLimitAuditLogger,
    ):
        self._registry = registry
        self._gate = gate
        self._audit = audit_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "tracked_users": self._registry.total_tracked_users(),
            "rejections_1h": self._audit.summary(3600.0),
            "rejections_24h": self._audit.summary(86400.0),
        }
```

## Comparison

| Approach | Per-User Tracking | Burst Allowance | Throttle Mode | Audit Trail | Dashboard |
|---|---|---|---|---|---|
| TokenBucketRateLimiter | No (single bucket) | Yes (capacity) | No | No | No |
| PerUserRateLimitRegistry | Yes (per-user) | Via policy | No | No | No |
| RateLimitGate | Via registry | Via registry | Yes | Via logger | No |
| RateLimitAuditLogger | No | No | No | Yes | No |
| PerUserRateLimitDashboard | No | No | No | No | Yes |

**Best for production**: Assign `tier`-based policies at authentication time and call `set_policy()` when a user's subscription changes — do not re-read the user's tier on every request. Use `RateLimitAction.THROTTLE` before `REJECT`: adding a 2-second delay at 80% utilization catches automation scripts (which ignore delays) while not disrupting human users who barely notice a 2-second slowdown. Set `burst_allowance` to 20-30% of `requests_per_minute` — this accommodates legitimate page loads and multi-step workflows that issue several requests in quick succession without allowing sustained abuse. Monitor `top_rejected_users` in `RateLimitAuditLogger`: a user appearing in the top 3 rejected users for three consecutive hours is likely running automated abuse, not experiencing a legitimate use case, and warrants account investigation.
