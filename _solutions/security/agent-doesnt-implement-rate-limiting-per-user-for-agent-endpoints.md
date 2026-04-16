---
title: "Agent Doesn't Implement Rate Limiting Per User for Agent Endpoints"
description: "Agent endpoints without per-user rate limiting allow a single user to exhaust LLM token budgets, trigger denial-of-service conditions, or perform enumeration attacks at unrestricted speed. Implement token-bucket rate limiting that enforces per-user request and token quotas, returns standard 429 responses with Retry-After headers, and logs burst patterns for abuse detection."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-rate-limiting-per-user-for-agent-endpoints
tags: [rate-limiting, token-bucket, per-user-quota, abuse-prevention, 429, denial-of-service]
symptoms:
  - "Single user exhausts daily LLM token budget in minutes via scripted requests"
  - "No 429 responses — every request is accepted regardless of prior volume"
  - "Abuse patterns are only visible after billing statement arrives"
  - "Rate limiting is applied globally but not per user, so one user can starve others"
  - "No Retry-After header guidance — clients retry immediately and amplify the load"
---

## Why This Happens

Global rate limiting protects the service but not individual users from each other. A single high-volume user hitting the global limit degrades the experience for all others. Per-user rate limiting requires storing a counter or token bucket per user identity, enforcing it on every request, and returning actionable error responses when the limit is reached. The token bucket algorithm is preferred over fixed windows because it allows short bursts while enforcing a long-run average rate, matching realistic usage patterns without punishing users for legitimate brief spikes.

## Solution 1: Token Bucket

```python
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """
    Classic token bucket: refills at `refill_rate` tokens/second up to `capacity`.
    `consume(n)` returns True if n tokens were available, False otherwise.
    """
    capacity: float
    refill_rate: float          # tokens per second
    tokens: float = field(init=False)
    last_refill: float = field(default_factory=time.time, init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def _refill(self) -> None:
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self, n: float = 1.0) -> bool:
        self._refill()
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def seconds_until_available(self, n: float = 1.0) -> float:
        self._refill()
        deficit = n - self.tokens
        if deficit <= 0:
            return 0.0
        return round(deficit / self.refill_rate, 2)

    def snapshot(self) -> dict:
        self._refill()
        return {
            "tokens": round(self.tokens, 2),
            "capacity": self.capacity,
            "refill_rate": self.refill_rate,
            "fill_pct": round(self.tokens / self.capacity * 100, 1),
        }
```

## Solution 2: Per-User Rate Limit Policy

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class UserRateLimitPolicy:
    """
    Defines request and token quotas for a user tier.
    requests_per_minute: max API calls per minute (burst allowed up to capacity).
    tokens_per_minute: max LLM tokens consumed per minute.
    burst_multiplier: how many minutes of capacity a user can spend in a burst.
    """
    tier: str
    requests_per_minute: float
    tokens_per_minute: float
    burst_multiplier: float = 2.0      # burst capacity = rate * multiplier
    hard_block_after_violations: int = 10  # block user after N consecutive violations

    def request_bucket_params(self) -> dict:
        return {
            "capacity": self.requests_per_minute * self.burst_multiplier,
            "refill_rate": self.requests_per_minute / 60.0,
        }

    def token_bucket_params(self) -> dict:
        return {
            "capacity": self.tokens_per_minute * self.burst_multiplier,
            "refill_rate": self.tokens_per_minute / 60.0,
        }


# Standard tiers
FREE_TIER = UserRateLimitPolicy(tier="free", requests_per_minute=10, tokens_per_minute=10_000)
PRO_TIER = UserRateLimitPolicy(tier="pro", requests_per_minute=60, tokens_per_minute=100_000)
ENTERPRISE_TIER = UserRateLimitPolicy(tier="enterprise", requests_per_minute=600, tokens_per_minute=1_000_000)
```

## Solution 3: Per-User Rate Limiter Store

```python
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple


@dataclass
class UserLimitState:
    request_bucket: TokenBucket
    token_bucket: TokenBucket
    policy: UserRateLimitPolicy
    violation_count: int = 0
    last_violation_at: Optional[float] = None
    hard_blocked: bool = False
    created_at: float = field(default_factory=time.time)


class PerUserRateLimiterStore:
    """
    Maintains per-user token buckets. Evicts stale entries after idle_evict_seconds.
    Thread-safe via per-store lock.
    """

    def __init__(
        self,
        default_policy: UserRateLimitPolicy = FREE_TIER,
        idle_evict_seconds: float = 3600.0,
    ):
        self._default_policy = default_policy
        self._evict_after = idle_evict_seconds
        self._store: Dict[str, UserLimitState] = {}
        self._lock = threading.Lock()

    def _make_state(self, policy: UserRateLimitPolicy) -> UserLimitState:
        rp = policy.request_bucket_params()
        tp = policy.token_bucket_params()
        return UserLimitState(
            request_bucket=TokenBucket(**rp),
            token_bucket=TokenBucket(**tp),
            policy=policy,
        )

    def get_or_create(
        self, user_id: str, policy: Optional[UserRateLimitPolicy] = None
    ) -> UserLimitState:
        with self._lock:
            if user_id not in self._store:
                self._store[user_id] = self._make_state(policy or self._default_policy)
            return self._store[user_id]

    def evict_stale(self) -> int:
        cutoff = time.time() - self._evict_after
        with self._lock:
            stale = [uid for uid, s in self._store.items() if s.created_at < cutoff]
            for uid in stale:
                del self._store[uid]
        return len(stale)

    def user_count(self) -> int:
        return len(self._store)
```

## Solution 4: Rate Limit Decision Engine

```python
import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class RateLimitDecision(str, Enum):
    ALLOW = "allow"
    DENY_REQUEST_RATE = "deny_request_rate"
    DENY_TOKEN_RATE = "deny_token_rate"
    DENY_HARD_BLOCK = "deny_hard_block"


@dataclass
class RateLimitResult:
    decision: RateLimitDecision
    user_id: str
    retry_after_seconds: float = 0.0
    remaining_requests: float = 0.0
    remaining_tokens: float = 0.0
    violation_count: int = 0

    def is_allowed(self) -> bool:
        return self.decision == RateLimitDecision.ALLOW

    def to_headers(self) -> dict:
        headers = {
            "X-RateLimit-Remaining-Requests": str(int(self.remaining_requests)),
            "X-RateLimit-Remaining-Tokens": str(int(self.remaining_tokens)),
        }
        if not self.is_allowed():
            headers["Retry-After"] = str(int(self.retry_after_seconds) + 1)
            headers["X-RateLimit-Reset"] = str(int(time.time() + self.retry_after_seconds))
        return headers


class RateLimitDecisionEngine:
    """
    Checks request + token buckets for a user and returns a RateLimitResult.
    Records violations and applies hard blocks after threshold is reached.
    """

    def __init__(self, store: PerUserRateLimiterStore):
        self._store = store

    def check(
        self,
        user_id: str,
        token_cost: float = 1.0,
        policy: Optional[UserRateLimitPolicy] = None,
    ) -> RateLimitResult:
        state = self._store.get_or_create(user_id, policy)

        if state.hard_blocked:
            return RateLimitResult(
                decision=RateLimitDecision.DENY_HARD_BLOCK,
                user_id=user_id,
                retry_after_seconds=3600.0,
                violation_count=state.violation_count,
            )

        # Check request rate
        if not state.request_bucket.consume(1.0):
            retry = state.request_bucket.seconds_until_available(1.0)
            self._record_violation(state)
            return RateLimitResult(
                decision=RateLimitDecision.DENY_REQUEST_RATE,
                user_id=user_id,
                retry_after_seconds=retry,
                remaining_requests=state.request_bucket.tokens,
                remaining_tokens=state.token_bucket.tokens,
                violation_count=state.violation_count,
            )

        # Check token budget
        if not state.token_bucket.consume(token_cost):
            retry = state.token_bucket.seconds_until_available(token_cost)
            # Refund the request token since we're denying
            state.request_bucket.tokens = min(
                state.request_bucket.capacity,
                state.request_bucket.tokens + 1.0,
            )
            self._record_violation(state)
            return RateLimitResult(
                decision=RateLimitDecision.DENY_TOKEN_RATE,
                user_id=user_id,
                retry_after_seconds=retry,
                remaining_requests=state.request_bucket.tokens,
                remaining_tokens=state.token_bucket.tokens,
                violation_count=state.violation_count,
            )

        return RateLimitResult(
            decision=RateLimitDecision.ALLOW,
            user_id=user_id,
            remaining_requests=state.request_bucket.tokens,
            remaining_tokens=state.token_bucket.tokens,
        )

    def _record_violation(self, state: UserLimitState) -> None:
        state.violation_count += 1
        state.last_violation_at = time.time()
        if state.violation_count >= state.policy.hard_block_after_violations:
            state.hard_blocked = True
```

## Solution 5: Rate Limiting Middleware

```python
import asyncio
from typing import Any, Awaitable, Callable, Optional


class RateLimitingMiddleware:
    """
    Async middleware that extracts user identity from a request context,
    runs the rate limit check, and either proceeds or returns a 429 response.
    Framework-agnostic: supply extract_user_id and build_429_response adapters.
    """

    def __init__(
        self,
        engine: RateLimitDecisionEngine,
        extract_user_id: Callable[[Any], str],
        extract_token_cost: Callable[[Any], float],
        build_429_response: Callable[[RateLimitResult], Any],
        get_user_policy: Optional[Callable[[str], Optional[UserRateLimitPolicy]]] = None,
    ):
        self._engine = engine
        self._extract_user = extract_user_id
        self._extract_cost = extract_token_cost
        self._build_429 = build_429_response
        self._get_policy = get_user_policy

    async def __call__(
        self,
        request: Any,
        call_next: Callable[[Any], Awaitable[Any]],
    ) -> Any:
        user_id = self._extract_user(request)
        token_cost = self._extract_cost(request)
        policy = self._get_policy(user_id) if self._get_policy else None

        result = self._engine.check(user_id, token_cost, policy)

        if not result.is_allowed():
            response = self._build_429(result)
            return response

        response = await call_next(request)
        return response
```

## Solution 6: Rate Limit Abuse Reporter

```python
import time
from collections import defaultdict
from typing import Dict, List


class RateLimitAbuseReporter:
    """
    Aggregates rate limit violations to surface abusive users.
    Distinguishes between occasional limit-hitting (normal) and
    sustained bursts (potential abuse or runaway automation).
    """

    def __init__(self, store: PerUserRateLimiterStore, window_seconds: float = 3600.0):
        self._store = store
        self._window = window_seconds

    def top_violators(self, n: int = 10) -> List[dict]:
        now = time.time()
        results = []
        for user_id, state in self._store._store.items():
            if state.violation_count == 0:
                continue
            recency = now - (state.last_violation_at or 0)
            results.append({
                "user_id": user_id,
                "tier": state.policy.tier,
                "violations": state.violation_count,
                "hard_blocked": state.hard_blocked,
                "last_violation_seconds_ago": round(recency, 0),
                "request_bucket": state.request_bucket.snapshot(),
                "token_bucket": state.token_bucket.snapshot(),
            })
        return sorted(results, key=lambda x: -x["violations"])[:n]

    def summary(self) -> dict:
        states = list(self._store._store.values())
        hard_blocked = sum(1 for s in states if s.hard_blocked)
        total_violations = sum(s.violation_count for s in states)
        by_tier: Dict[str, int] = defaultdict(int)
        for s in states:
            by_tier[s.policy.tier] += s.violation_count
        return {
            "total_tracked_users": len(states),
            "hard_blocked_users": hard_blocked,
            "total_violations": total_violations,
            "violations_by_tier": dict(by_tier),
            "top_violators": self.top_violators(5),
        }
```

## Comparison

| Approach | Per-User Buckets | Request Rate | Token Rate | Hard Block | 429 Headers | Abuse Detection |
|---|---|---|---|---|---|---|
| TokenBucket | No (single) | Yes | Yes | No | No | No |
| PerUserRateLimiterStore | Yes | No | No | No | No | No |
| RateLimitDecisionEngine | Via store | Yes | Yes | Yes | No | No |
| RateLimitingMiddleware | Via engine | Via engine | Via engine | Via engine | Yes | No |
| RateLimitAbuseReporter | No | No | No | No | No | Yes |

**Best for production**: Apply two separate token buckets per user — one for request rate and one for LLM token consumption — because a user can stay within request limits while still exhausting token budgets with long prompts. Set `burst_multiplier=2.0` to allow short bursts without triggering false positives on legitimate users. Always return `Retry-After` headers so well-behaved clients back off rather than retry immediately. Monitor `RateLimitAbuseReporter.summary()` for users who accumulate more than 50 violations per hour — this almost always indicates a runaway automation that should be investigated rather than just blocked.
