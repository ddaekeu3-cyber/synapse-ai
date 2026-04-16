---
title: "Agent Doesn't Implement Rate Limiting Per User for Tool Calls"
description: "Agents that apply a single global rate limit across all users allow one abusive session to exhaust the quota for every other user — and allow a compromised session token to drive unlimited tool executions until the underlying API bills accumulate. Implement per-user rate limiting with token-bucket or sliding-window counters that enforce independent quotas per user ID, with separate limits for different tool cost tiers."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-rate-limiting-per-user-for-tool-calls
tags: [rate-limiting, per-user-quota, token-bucket, sliding-window, abuse-prevention, tool-call-throttling]
symptoms:
  - "One active session can monopolize all available tool call capacity"
  - "A leaked session token drives unlimited expensive tool calls until manually revoked"
  - "Global rate limit fires for legitimate users when one user spikes"
  - "No differentiation between cheap read tools and expensive write or external-API tools"
  - "Rate limit counters reset on agent restart, allowing burst attacks after deploys"
---

## Why This Happens

Global rate limits protect the system as a whole but give each user an uncapped share of that budget. A single aggressive or compromised session can consume the entire global allowance, rate-limiting every other user. Per-user limiting requires a counter store indexed by user ID — either in-process (suitable for single-instance deployments) or in Redis (required for multi-instance). Separate limits per tool cost tier prevent a user from staying under the cheap-tool limit while still executing an unbounded number of expensive external API calls.

## Solution 1: Rate Limit Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class ToolCostTier(str, Enum):
    CHEAP = "cheap"       # reads, lookups, local computation
    STANDARD = "standard" # LLM sub-calls, cached retrievals
    EXPENSIVE = "expensive" # external API writes, code execution, file I/O


@dataclass
class TierLimit:
    requests_per_minute: int
    requests_per_hour: int
    burst_allowance: int = 0   # extra requests permitted in a short burst


@dataclass
class UserRateLimitPolicy:
    user_id: str
    tier_limits: Dict[ToolCostTier, TierLimit] = field(default_factory=dict)
    global_per_minute: int = 60
    global_per_hour: int = 500
    suspended: bool = False
    suspend_reason: str = ""

    @classmethod
    def default(cls, user_id: str) -> "UserRateLimitPolicy":
        return cls(
            user_id=user_id,
            tier_limits={
                ToolCostTier.CHEAP: TierLimit(requests_per_minute=30, requests_per_hour=300, burst_allowance=5),
                ToolCostTier.STANDARD: TierLimit(requests_per_minute=15, requests_per_hour=150),
                ToolCostTier.EXPENSIVE: TierLimit(requests_per_minute=5, requests_per_hour=30),
            },
        )
```

## Solution 2: Sliding Window Counter

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class SlidingWindowCounter:
    """
    Thread-safe sliding window counter for rate limiting.
    Evicts timestamps outside the window on each check.
    """

    def __init__(self, window_seconds: float):
        self._window = window_seconds
        self._timestamps: Deque[float] = deque()
        self._lock = Lock()

    def increment_and_check(self, limit: int) -> Tuple[bool, int]:
        """
        Records a new event and returns (allowed, current_count).
        allowed=False means the limit was exceeded before recording.
        """
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            # evict stale
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            current = len(self._timestamps)
            if current >= limit:
                return False, current
            self._timestamps.append(now)
            return True, current + 1

    def current_count(self, window_seconds: Optional[float] = None) -> int:
        now = time.time()
        cutoff = now - (window_seconds or self._window)
        with self._lock:
            return sum(1 for ts in self._timestamps if ts >= cutoff)

    def reset(self) -> None:
        with self._lock:
            self._timestamps.clear()
```

## Solution 3: Per-User Rate Limiter

```python
import time
from threading import Lock
from typing import Dict, Optional, Tuple


class PerUserRateLimiter:
    """
    Maintains independent sliding window counters per (user_id, tier, window).
    Cleans up inactive user counters to prevent unbounded memory growth.
    """

    def __init__(
        self,
        default_policy_fn=None,
        cleanup_interval_seconds: float = 300.0,
        inactivity_evict_seconds: float = 3600.0,
    ):
        self._counters: Dict[str, SlidingWindowCounter] = {}
        self._last_access: Dict[str, float] = {}
        self._policies: Dict[str, UserRateLimitPolicy] = {}
        self._default_policy_fn = default_policy_fn or UserRateLimitPolicy.default
        self._cleanup_interval = cleanup_interval_seconds
        self._inactivity_evict = inactivity_evict_seconds
        self._last_cleanup = time.time()
        self._lock = Lock()

    def _counter_key(self, user_id: str, tier: ToolCostTier, window: str) -> str:
        return f"{user_id}:{tier.value}:{window}"

    def _get_or_create(self, key: str, window_seconds: float) -> SlidingWindowCounter:
        if key not in self._counters:
            self._counters[key] = SlidingWindowCounter(window_seconds)
        self._last_access[key] = time.time()
        return self._counters[key]

    def set_policy(self, policy: UserRateLimitPolicy) -> None:
        with self._lock:
            self._policies[policy.user_id] = policy

    def check(
        self,
        user_id: str,
        tier: ToolCostTier = ToolCostTier.STANDARD,
    ) -> Tuple[bool, str]:
        """
        Returns (allowed, reason). reason is empty string if allowed.
        """
        with self._lock:
            self._maybe_cleanup()
            policy = self._policies.get(user_id) or self._default_policy_fn(user_id)

            if policy.suspended:
                return False, f"user suspended: {policy.suspend_reason}"

            tier_limit = policy.tier_limits.get(tier)
            if tier_limit:
                # Per-minute check
                min_key = self._counter_key(user_id, tier, "1m")
                min_counter = self._get_or_create(min_key, 60.0)
                allowed, _ = min_counter.increment_and_check(tier_limit.requests_per_minute)
                if not allowed:
                    return False, f"rate limit: {tier.value} tier per-minute exceeded"

                # Per-hour check
                hr_key = self._counter_key(user_id, tier, "1h")
                hr_counter = self._get_or_create(hr_key, 3600.0)
                allowed, _ = hr_counter.increment_and_check(tier_limit.requests_per_hour)
                if not allowed:
                    return False, f"rate limit: {tier.value} tier per-hour exceeded"

            return True, ""

    def _maybe_cleanup(self) -> None:
        if time.time() - self._last_cleanup < self._cleanup_interval:
            return
        cutoff = time.time() - self._inactivity_evict
        stale_keys = [k for k, t in self._last_access.items() if t < cutoff]
        for k in stale_keys:
            self._counters.pop(k, None)
            self._last_access.pop(k, None)
        self._last_cleanup = time.time()
```

## Solution 4: Rate-Limited Tool Dispatcher

```python
from typing import Any, Callable, Dict, Optional


TOOL_TIER_MAP: Dict[str, ToolCostTier] = {}


def register_tool_tier(tool_name: str, tier: ToolCostTier) -> None:
    TOOL_TIER_MAP[tool_name] = tier


class RateLimitedToolDispatcher:
    """
    Wraps tool execution with per-user rate limit enforcement.
    Raises RateLimitExceeded before the tool function is called.
    """

    def __init__(self, limiter: PerUserRateLimiter):
        self._limiter = limiter

    async def dispatch(
        self,
        user_id: str,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        tier = TOOL_TIER_MAP.get(tool_name, ToolCostTier.STANDARD)
        allowed, reason = self._limiter.check(user_id, tier)
        if not allowed:
            raise RateLimitExceeded(user_id=user_id, tool_name=tool_name, reason=reason)
        return await tool_fn(*args, **kwargs)


class RateLimitExceeded(Exception):
    def __init__(self, user_id: str, tool_name: str, reason: str):
        super().__init__(f"rate limit exceeded for user={user_id} tool={tool_name}: {reason}")
        self.user_id = user_id
        self.tool_name = tool_name
        self.reason = reason
```

## Solution 5: Rate Limit Violation Auditor

```python
import time
from collections import defaultdict
from typing import Dict, List


class RateLimitViolationAuditor:
    """
    Records rate limit violations for abuse detection.
    Flags users who consistently hit limits (indicating automation or abuse).
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, exc: RateLimitExceeded) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "user_id": exc.user_id,
            "tool_name": exc.tool_name,
            "reason": exc.reason,
        })

    def top_violators(
        self,
        window_seconds: float = 3600.0,
        top_n: int = 10,
    ) -> List[dict]:
        cutoff = time.time() - window_seconds
        counts: Dict[str, int] = defaultdict(int)
        for r in self._records:
            if r["ts"] >= cutoff:
                counts[r["user_id"]] += 1
        sorted_users = sorted(counts.items(), key=lambda x: x[1], reverse=True)
        return [{"user_id": u, "violations": v} for u, v in sorted_users[:top_n]]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        unique_users = len({r["user_id"] for r in recent})
        return {
            "window_seconds": window_seconds,
            "total_violations": len(recent),
            "unique_users_affected": unique_users,
            "top_violators": self.top_violators(window_seconds, top_n=5),
        }
```

## Solution 6: Rate Limit Dashboard

```python
import time


class RateLimitDashboard:
    """
    Combines per-user limiter state and violation audit into a single report.
    """

    def __init__(
        self,
        limiter: PerUserRateLimiter,
        auditor: RateLimitViolationAuditor,
    ):
        self._limiter = limiter
        self._auditor = auditor

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "violations": self._auditor.summary(window_seconds),
        }
```

## Comparison

| Approach | Per-User Counters | Tier-Based Limits | Suspension | Violation Audit | Dashboard |
|---|---|---|---|---|---|
| SlidingWindowCounter | No (single counter) | No | No | No | No |
| PerUserRateLimiter | Yes | Yes (3 tiers) | Yes | No | No |
| RateLimitedToolDispatcher | Via limiter | Via tier map | Via limiter | No | No |
| RateLimitViolationAuditor | No | No | No | Yes | No |
| RateLimitDashboard | No | No | No | No | Yes |

**Best for production**: Use Redis with sorted sets for the sliding window counters in multi-instance deployments — all instances share a single counter per user, preventing a user from bypassing limits by hitting different pods. Set `EXPENSIVE` tier limits conservatively (5/min, 30/hr) for any tool that calls external APIs or executes code, since these carry both cost and security risk. Monitor `RateLimitViolationAuditor.top_violators()` hourly — users with 50+ violations per hour are likely running automated scripts and should trigger an account review workflow.
