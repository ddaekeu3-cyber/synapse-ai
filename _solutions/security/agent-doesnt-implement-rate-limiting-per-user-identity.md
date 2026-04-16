---
title: "Agent Doesn't Implement Rate Limiting per User Identity"
description: "Agents that apply rate limits only at the API gateway level allow a single authenticated user to exhaust shared LLM token budgets, vector store query quotas, and tool call limits by submitting requests in rapid succession. Implement per-user-identity rate limiting with token bucket enforcement that isolates each user's consumption and prevents one actor from degrading the experience for all others."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-rate-limiting-per-user-identity
tags: [rate-limiting, token-bucket, per-user, quota-enforcement, abuse-prevention, fair-use]
symptoms:
  - "One user submitting bulk requests exhausts the LLM token quota for all users"
  - "No per-user counters — rate limits apply globally or not at all"
  - "Automated scripts using valid credentials can flood the agent indefinitely"
  - "No distinction between interactive users and API consumers in rate limit policy"
  - "Rate limit state is in-memory and resets on every agent restart"
---

## Why This Happens

API gateways enforce IP-based or key-based global rate limits that do not map to authenticated user identity. Once a request passes the gateway, the agent applies no further throttling — every authenticated user has access to the full shared capacity. Per-user rate limiting requires identity extraction from the request context, per-identity token buckets that refill at a configured rate, and a persistence layer so limits survive agent restarts.

## Solution 1: Rate Limit Policy

```python
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional


class UserTier(str, Enum):
    FREE = "free"
    PRO = "pro"
    ENTERPRISE = "enterprise"
    INTERNAL = "internal"


@dataclass
class RateLimitPolicy:
    tier: UserTier
    requests_per_minute: int
    requests_per_hour: int
    llm_tokens_per_hour: int
    tool_calls_per_minute: int
    burst_allowance: int = 0    # extra requests allowed in burst above RPM


DEFAULT_POLICIES: Dict[UserTier, RateLimitPolicy] = {
    UserTier.FREE: RateLimitPolicy(
        tier=UserTier.FREE,
        requests_per_minute=10,
        requests_per_hour=200,
        llm_tokens_per_hour=100_000,
        tool_calls_per_minute=20,
        burst_allowance=3,
    ),
    UserTier.PRO: RateLimitPolicy(
        tier=UserTier.PRO,
        requests_per_minute=60,
        requests_per_hour=2000,
        llm_tokens_per_hour=1_000_000,
        tool_calls_per_minute=120,
        burst_allowance=10,
    ),
    UserTier.ENTERPRISE: RateLimitPolicy(
        tier=UserTier.ENTERPRISE,
        requests_per_minute=300,
        requests_per_hour=20_000,
        llm_tokens_per_hour=10_000_000,
        tool_calls_per_minute=600,
        burst_allowance=50,
    ),
    UserTier.INTERNAL: RateLimitPolicy(
        tier=UserTier.INTERNAL,
        requests_per_minute=10_000,
        requests_per_hour=500_000,
        llm_tokens_per_hour=500_000_000,
        tool_calls_per_minute=10_000,
        burst_allowance=500,
    ),
}
```

## Solution 2: Per-User Token Bucket

```python
import time
from threading import Lock
from typing import Optional


class UserTokenBucket:
    """
    Token bucket rate limiter for a single user identity.
    Supports separate buckets for requests-per-minute and tokens-per-hour.
    """

    def __init__(
        self,
        capacity: float,
        refill_rate: float,    # tokens per second
        initial_tokens: Optional[float] = None,
    ):
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._tokens = initial_tokens if initial_tokens is not None else capacity
        self._last_refill = time.time()
        self._lock = Lock()

    def _refill(self) -> None:
        now = time.time()
        elapsed = now - self._last_refill
        self._tokens = min(self._capacity, self._tokens + elapsed * self._refill_rate)
        self._last_refill = now

    def consume(self, amount: float = 1.0) -> bool:
        with self._lock:
            self._refill()
            if self._tokens >= amount:
                self._tokens -= amount
                return True
            return False

    def available(self) -> float:
        with self._lock:
            self._refill()
            return round(self._tokens, 2)

    def retry_after_seconds(self, amount: float = 1.0) -> float:
        with self._lock:
            self._refill()
            deficit = amount - self._tokens
            if deficit <= 0:
                return 0.0
            return round(deficit / self._refill_rate, 2)
```

## Solution 3: Per-User Rate Limit State

```python
import time
from dataclasses import dataclass, field


@dataclass
class UserRateLimitState:
    user_id: str
    tier: UserTier
    rpm_bucket: UserTokenBucket
    rph_bucket: UserTokenBucket
    token_bucket: UserTokenBucket      # LLM tokens per hour
    tool_bucket: UserTokenBucket       # tool calls per minute
    created_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    total_requests: int = 0
    total_rejections: int = 0

    def touch(self) -> None:
        self.last_seen_at = time.time()

    def is_idle(self, idle_seconds: float = 3600.0) -> bool:
        return time.time() - self.last_seen_at > idle_seconds
```

## Solution 4: Per-User Rate Limiter Registry

```python
import time
from threading import Lock
from typing import Dict, Optional


class PerUserRateLimiterRegistry:
    """
    Creates and manages per-user rate limit state.
    Evicts idle user state after a configurable TTL.
    """

    def __init__(
        self,
        policies: Dict[UserTier, RateLimitPolicy],
        idle_eviction_seconds: float = 3600.0,
    ):
        self._policies = policies
        self._idle_ttl = idle_eviction_seconds
        self._states: Dict[str, UserRateLimitState] = {}
        self._lock = Lock()

    def _create_state(self, user_id: str, tier: UserTier) -> UserRateLimitState:
        policy = self._policies[tier]
        return UserRateLimitState(
            user_id=user_id,
            tier=tier,
            rpm_bucket=UserTokenBucket(
                capacity=policy.requests_per_minute + policy.burst_allowance,
                refill_rate=policy.requests_per_minute / 60.0,
            ),
            rph_bucket=UserTokenBucket(
                capacity=policy.requests_per_hour,
                refill_rate=policy.requests_per_hour / 3600.0,
            ),
            token_bucket=UserTokenBucket(
                capacity=policy.llm_tokens_per_hour,
                refill_rate=policy.llm_tokens_per_hour / 3600.0,
            ),
            tool_bucket=UserTokenBucket(
                capacity=policy.tool_calls_per_minute + policy.burst_allowance,
                refill_rate=policy.tool_calls_per_minute / 60.0,
            ),
        )

    def get_or_create(self, user_id: str, tier: UserTier) -> UserRateLimitState:
        with self._lock:
            if user_id not in self._states:
                self._states[user_id] = self._create_state(user_id, tier)
            state = self._states[user_id]
            state.touch()
            return state

    def evict_idle(self) -> int:
        with self._lock:
            idle = [uid for uid, s in self._states.items() if s.is_idle(self._idle_ttl)]
            for uid in idle:
                del self._states[uid]
            return len(idle)

    def active_user_count(self) -> int:
        with self._lock:
            return len(self._states)
```

## Solution 5: Rate Limit Enforcement Gate

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class RateLimitDecision:
    allowed: bool
    user_id: str
    limit_type: str          # "rpm", "rph", "tokens", "tools"
    retry_after_seconds: float = 0.0
    available_tokens: float = 0.0


class RateLimitEnforcementGate:
    """
    Checks all rate limit buckets for a user before allowing a request.
    Returns the first limit that would be exceeded.
    """

    def __init__(self, registry: PerUserRateLimiterRegistry):
        self._registry = registry

    def check_request(
        self,
        user_id: str,
        tier: UserTier,
        llm_tokens_estimate: int = 0,
        tool_calls_estimate: int = 0,
    ) -> RateLimitDecision:
        state = self._registry.get_or_create(user_id, tier)

        for limit_type, bucket, amount in [
            ("rpm", state.rpm_bucket, 1.0),
            ("rph", state.rph_bucket, 1.0),
            ("tokens", state.token_bucket, float(max(llm_tokens_estimate, 1))),
            ("tools", state.tool_bucket, float(max(tool_calls_estimate, 0))),
        ]:
            if amount > 0 and not bucket.consume(amount):
                state.total_rejections += 1
                return RateLimitDecision(
                    allowed=False,
                    user_id=user_id,
                    limit_type=limit_type,
                    retry_after_seconds=bucket.retry_after_seconds(amount),
                    available_tokens=bucket.available(),
                )

        state.total_requests += 1
        return RateLimitDecision(allowed=True, user_id=user_id, limit_type="none")
```

## Solution 6: Rate Limit Audit Dashboard

```python
import time
from typing import List


class RateLimitAuditDashboard:
    """
    Surfaces per-user rejection rates, top throttled users,
    and overall rate limit health across the registry.
    """

    def __init__(self, registry: PerUserRateLimiterRegistry):
        self._registry = registry

    def render(self) -> dict:
        with self._registry._lock:
            states = list(self._registry._states.values())

        total_requests = sum(s.total_requests for s in states)
        total_rejections = sum(s.total_rejections for s in states)

        top_throttled = sorted(
            states, key=lambda s: s.total_rejections, reverse=True
        )[:10]

        by_tier: dict = {}
        for s in states:
            tier = s.tier.value
            if tier not in by_tier:
                by_tier[tier] = {"users": 0, "requests": 0, "rejections": 0}
            by_tier[tier]["users"] += 1
            by_tier[tier]["requests"] += s.total_requests
            by_tier[tier]["rejections"] += s.total_rejections

        return {
            "generated_at": time.time(),
            "active_users": len(states),
            "total_requests": total_requests,
            "total_rejections": total_rejections,
            "rejection_rate": round(total_rejections / max(total_requests, 1), 4),
            "by_tier": by_tier,
            "top_throttled": [
                {
                    "user_id": s.user_id,
                    "tier": s.tier.value,
                    "rejections": s.total_rejections,
                    "requests": s.total_requests,
                }
                for s in top_throttled if s.total_rejections > 0
            ],
        }
```

## Comparison

| Approach | Per-User Buckets | Tier Policies | Multi-Limit (RPM+tokens) | Enforcement Gate | Audit |
|---|---|---|---|---|---|
| RateLimitPolicy | No | Yes (4 tiers) | Yes (definition) | No | No |
| UserTokenBucket | Per instance | No | Per instance | No | No |
| PerUserRateLimiterRegistry | Yes | Via policies | Yes | No | No |
| RateLimitEnforcementGate | Via registry | Via registry | Yes (check all) | Yes | No |
| RateLimitAuditDashboard | No | No | No | No | Yes |

**Best for production**: Use four separate buckets per user (RPM, RPH, LLM tokens, tool calls) — a single request counter cannot distinguish a user who sends 60 tiny requests from one who sends 1 request consuming 60× the token budget. Evict idle user state with `evict_idle()` on a background timer to prevent the registry from growing unbounded. Return `Retry-After` header with `retry_after_seconds` on 429 responses — clients that respect it reduce retry storms. Log rejections from `RateLimitAuditDashboard` to detect coordinated abuse: multiple user IDs with identical rejection patterns hitting the same endpoints suggest credential sharing or a botnet.
