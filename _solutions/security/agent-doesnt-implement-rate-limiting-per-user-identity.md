---
title: "Agent Doesn't Implement Rate Limiting Per User Identity"
description: "Agents that apply a single global rate limit — or no rate limit at all — allow one abusive user to exhaust the LLM API quota for all users, and allow credential-stuffing bots to probe the agent at unlimited speed. Implement per-identity rate limiting with a sliding window algorithm, tier-based limits, and automatic temporary ban after sustained abuse."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-rate-limiting-per-user-identity
tags: [rate-limiting, per-user-quota, sliding-window, abuse-prevention, api-quota, token-bucket]
symptoms:
  - "One user's burst of requests causes 429s for all other users"
  - "No distinction between free-tier and paid-tier request limits"
  - "Automated scrapers can call the agent at thousands of requests per minute"
  - "LLM API quota exhausted by a single session — no per-identity guardrail"
  - "No automatic escalation from rate-limited to temporarily banned for repeat offenders"
---

## Why This Happens

Global rate limiting protects the LLM API from total overload but does not prevent one identity from monopolizing capacity. Per-identity limiting requires maintaining per-user state — a sliding window counter, a token bucket, or a fixed window counter — keyed to a stable identity (user ID, API key, IP address). Without this, a single authenticated user can issue thousands of requests per minute until the upstream quota is exhausted. The sliding window algorithm is preferred over fixed windows because it prevents the burst-at-boundary exploit where a user fires `limit` requests at 23:59:59 and another `limit` at 00:00:00.

## Solution 1: Rate Limit Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class RateLimitTier(str, Enum):
    FREE = "free"
    STANDARD = "standard"
    PREMIUM = "premium"
    INTERNAL = "internal"   # no limit


@dataclass(frozen=True)
class RateLimitPolicy:
    tier: RateLimitTier
    requests_per_minute: int
    requests_per_hour: int
    requests_per_day: int
    burst_allowance: int = 0    # extra requests allowed in a short burst
    ban_threshold_violations: int = 10   # violations before temp ban
    ban_duration_s: float = 3600.0


DEFAULT_POLICIES: Dict[RateLimitTier, RateLimitPolicy] = {
    RateLimitTier.FREE: RateLimitPolicy(
        tier=RateLimitTier.FREE,
        requests_per_minute=10,
        requests_per_hour=100,
        requests_per_day=500,
        burst_allowance=5,
        ban_threshold_violations=10,
        ban_duration_s=3600.0,
    ),
    RateLimitTier.STANDARD: RateLimitPolicy(
        tier=RateLimitTier.STANDARD,
        requests_per_minute=60,
        requests_per_hour=1000,
        requests_per_day=10000,
        burst_allowance=20,
        ban_threshold_violations=30,
        ban_duration_s=1800.0,
    ),
    RateLimitTier.PREMIUM: RateLimitPolicy(
        tier=RateLimitTier.PREMIUM,
        requests_per_minute=300,
        requests_per_hour=5000,
        requests_per_day=50000,
        burst_allowance=100,
        ban_threshold_violations=100,
        ban_duration_s=600.0,
    ),
    RateLimitTier.INTERNAL: RateLimitPolicy(
        tier=RateLimitTier.INTERNAL,
        requests_per_minute=999999,
        requests_per_hour=999999,
        requests_per_day=999999,
    ),
}
```

## Solution 2: Sliding Window Counter

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class SlidingWindowCounter:
    """
    Counts requests within a sliding time window using a timestamp deque.
    Thread-safe for use in a shared per-identity rate limiter.
    """

    def __init__(self, window_seconds: float, limit: int):
        self._window = window_seconds
        self._limit = limit
        self._timestamps: Deque[float] = deque()
        self._lock = Lock()

    def record_and_check(self) -> Tuple[bool, int]:
        """
        Record this request and return (allowed, current_count).
        allowed=False means the limit is exceeded.
        """
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            # Evict timestamps outside the window
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            count = len(self._timestamps)
            if count >= self._limit:
                return False, count
            self._timestamps.append(now)
            return True, count + 1

    def current_count(self) -> int:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            return len(self._timestamps)

    def reset(self) -> None:
        with self._lock:
            self._timestamps.clear()
```

## Solution 3: Per-Identity Rate Limiter

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Optional, Tuple


@dataclass
class RateLimitDecision:
    allowed: bool
    identity: str
    tier: RateLimitTier
    violated_window: str = ""   # "minute", "hour", "day", or ""
    retry_after_s: float = 0.0
    is_banned: bool = False
    current_minute_count: int = 0
    current_hour_count: int = 0


class PerIdentityRateLimiter:
    """
    Maintains per-identity sliding window counters for minute/hour/day windows.
    Automatically escalates repeat violators to a temporary ban.
    """

    def __init__(self, policies: Dict[RateLimitTier, RateLimitPolicy]):
        self._policies = policies
        self._minute_windows: Dict[str, SlidingWindowCounter] = {}
        self._hour_windows: Dict[str, SlidingWindowCounter] = {}
        self._day_windows: Dict[str, SlidingWindowCounter] = {}
        self._violation_counts: Dict[str, int] = {}
        self._bans: Dict[str, float] = {}   # identity -> ban_expires_at
        self._lock = Lock()

    def _get_policy(self, tier: RateLimitTier) -> RateLimitPolicy:
        return self._policies.get(tier, self._policies[RateLimitTier.FREE])

    def _ensure_windows(self, identity: str, policy: RateLimitPolicy) -> None:
        if identity not in self._minute_windows:
            self._minute_windows[identity] = SlidingWindowCounter(60.0, policy.requests_per_minute + policy.burst_allowance)
            self._hour_windows[identity] = SlidingWindowCounter(3600.0, policy.requests_per_hour)
            self._day_windows[identity] = SlidingWindowCounter(86400.0, policy.requests_per_day)

    def check(self, identity: str, tier: RateLimitTier) -> RateLimitDecision:
        policy = self._get_policy(tier)

        if tier == RateLimitTier.INTERNAL:
            return RateLimitDecision(allowed=True, identity=identity, tier=tier)

        # Check ban
        with self._lock:
            ban_expires = self._bans.get(identity, 0.0)
            if time.time() < ban_expires:
                return RateLimitDecision(
                    allowed=False,
                    identity=identity,
                    tier=tier,
                    is_banned=True,
                    retry_after_s=round(ban_expires - time.time(), 1),
                )
            self._ensure_windows(identity, policy)

        # Check windows (outside lock — SlidingWindowCounter is self-locking)
        min_ok, min_count = self._minute_windows[identity].record_and_check()
        if not min_ok:
            self._record_violation(identity, policy)
            return RateLimitDecision(
                allowed=False, identity=identity, tier=tier,
                violated_window="minute", retry_after_s=60.0,
                current_minute_count=min_count,
            )

        hr_ok, hr_count = self._hour_windows[identity].record_and_check()
        if not hr_ok:
            self._record_violation(identity, policy)
            return RateLimitDecision(
                allowed=False, identity=identity, tier=tier,
                violated_window="hour", retry_after_s=3600.0,
                current_minute_count=min_count,
            )

        day_ok, _ = self._day_windows[identity].record_and_check()
        if not day_ok:
            self._record_violation(identity, policy)
            return RateLimitDecision(
                allowed=False, identity=identity, tier=tier,
                violated_window="day", retry_after_s=86400.0,
                current_minute_count=min_count,
            )

        return RateLimitDecision(
            allowed=True, identity=identity, tier=tier,
            current_minute_count=min_count, current_hour_count=hr_count,
        )

    def _record_violation(self, identity: str, policy: RateLimitPolicy) -> None:
        with self._lock:
            count = self._violation_counts.get(identity, 0) + 1
            self._violation_counts[identity] = count
            if count >= policy.ban_threshold_violations:
                self._bans[identity] = time.time() + policy.ban_duration_s
                self._violation_counts[identity] = 0

    def lift_ban(self, identity: str) -> None:
        with self._lock:
            self._bans.pop(identity, None)
            self._violation_counts.pop(identity, None)
```

## Solution 4: Rate Limit Headers Builder

```python
from typing import Dict


class RateLimitHeadersBuilder:
    """
    Produces standard rate limit HTTP response headers from a decision.
    Compatible with the RateLimit-* header draft (IETF draft-ietf-httpapi-ratelimit-headers).
    """

    @staticmethod
    def build(decision: RateLimitDecision, policy: RateLimitPolicy) -> Dict[str, str]:
        headers: Dict[str, str] = {
            "X-RateLimit-Tier": decision.tier.value,
            "X-RateLimit-Limit-Minute": str(policy.requests_per_minute),
            "X-RateLimit-Remaining-Minute": str(
                max(policy.requests_per_minute - decision.current_minute_count, 0)
            ),
        }
        if not decision.allowed:
            headers["Retry-After"] = str(int(decision.retry_after_s))
            if decision.is_banned:
                headers["X-RateLimit-Ban"] = "true"
                headers["X-RateLimit-Ban-Reason"] = "repeated_violations"
            else:
                headers["X-RateLimit-Violated-Window"] = decision.violated_window
        return headers
```

## Solution 5: Rate Limit Audit Logger

```python
import time
from typing import List


class RateLimitAuditLogger:
    """
    Records rate limit violations and bans for security review.
    Surfaces identities with the most violations for manual investigation.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, decision: RateLimitDecision) -> None:
        if decision.allowed:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "identity": decision.identity,
            "tier": decision.tier.value,
            "violated_window": decision.violated_window,
            "is_banned": decision.is_banned,
            "retry_after_s": decision.retry_after_s,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "violations": 0}

        identity_counts: dict = {}
        for r in recent:
            identity_counts[r["identity"]] = identity_counts.get(r["identity"], 0) + 1

        top_offenders = sorted(identity_counts.items(), key=lambda x: -x[1])[:10]
        return {
            "window_seconds": window_seconds,
            "violations": len(recent),
            "banned_events": sum(1 for r in recent if r["is_banned"]),
            "unique_identities": len(identity_counts),
            "top_offenders": [{"identity": i, "count": c} for i, c in top_offenders],
        }
```

## Solution 6: Rate Limiting Dashboard

```python
import time


class RateLimitingDashboard:
    """
    Combines limiter state, violation audit, and policy summary
    into a single operational view.
    """

    def __init__(
        self,
        limiter: PerIdentityRateLimiter,
        audit_logger: RateLimitAuditLogger,
        policies: dict,
    ):
        self._limiter = limiter
        self._audit = audit_logger
        self._policies = policies

    def render(self) -> dict:
        bans = {
            identity: round(expires - time.time(), 1)
            for identity, expires in self._limiter._bans.items()
            if expires > time.time()
        }
        return {
            "generated_at": time.time(),
            "active_bans": bans,
            "active_ban_count": len(bans),
            "policy_summary": {
                tier.value: {
                    "rpm": p.requests_per_minute,
                    "rph": p.requests_per_hour,
                    "rpd": p.requests_per_day,
                }
                for tier, p in self._policies.items()
                if tier != RateLimitTier.INTERNAL
            },
            "last_hour_violations": self._audit.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Sliding Window | Tier-Based Limits | Auto-Ban | HTTP Headers | Audit Log |
|---|---|---|---|---|---|
| SlidingWindowCounter | Yes (deque-based) | No | No | No | No |
| PerIdentityRateLimiter | Yes (3 windows) | Yes | Yes | No | No |
| RateLimitHeadersBuilder | No | No | No | Yes (IETF draft) | No |
| RateLimitAuditLogger | No | No | No | No | Yes |
| RateLimitingDashboard | No | No | No | No | Via audit |

**Best for production**: Back `PerIdentityRateLimiter` with Redis using Lua scripts for atomic sliding window increments in multi-instance deployments — the in-memory version above will allow `N × limit` requests across N instances. Set `ban_threshold_violations=10` conservatively for free-tier users; legitimate users rarely hit the rate limit more than once per hour. Return `Retry-After` on every 429 — well-behaved clients will back off, and the absence of back-off is itself a signal that the caller is a bot. Monitor `top_offenders` from `RateLimitAuditLogger.summary()` daily: persistent repeat offenders should be escalated to account suspension rather than temporary bans.
