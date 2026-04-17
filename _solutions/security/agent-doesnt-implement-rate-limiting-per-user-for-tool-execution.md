---
title: "Agent Doesn't Implement Rate Limiting Per User for Tool Execution"
description: "Agents that apply no per-user rate limits on tool execution allow a single user to monopolize expensive resources — search APIs, LLM calls, database queries — through rapid automated requests. Implement per-user tool execution rate limiting with token bucket or sliding window counters, per-tool quotas, and graduated enforcement from throttling to temporary suspension."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-rate-limiting-per-user-for-tool-execution
tags: [rate-limiting, per-user-quota, token-bucket, tool-abuse-prevention, resource-protection, sliding-window]
symptoms:
  - "Single user can exhaust search API quota for all users within minutes"
  - "No per-user counter on tool executions — only global rate limits"
  - "Automated scripts hitting the agent bypass request-level throttling via tool chaining"
  - "Expensive tools (LLM, vector search) have no individual user caps"
  - "No graduated response — users go from unlimited to hard-blocked with no warning"
---

## Why This Happens

Global rate limits protect the service from aggregate overload but not from individual abuse: one user running automated requests can consume the entire quota while other users are throttled. Per-user limits require associating each tool call with an identity, maintaining per-identity counters in a shared store, and checking limits before dispatching. The challenge is that tool calls happen inside the agent loop where user identity may not be explicitly threaded — it must be injected at the session level and propagated to the rate-limit check point.

## Solution 1: Per-User Rate Limit Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional


class RateLimitScope(str, Enum):
    PER_TOOL = "per_tool"           # separate counter per (user, tool) pair
    PER_USER = "per_user"           # single counter for all tools per user
    PER_TOOL_CATEGORY = "per_tool_category"  # counter per (user, category) pair


@dataclass
class ToolRateLimit:
    requests_per_window: int
    window_seconds: float
    burst_allowance: int = 0        # extra requests allowed in burst
    scope: RateLimitScope = RateLimitScope.PER_TOOL


@dataclass
class UserRateLimitPolicy:
    """
    Defines rate limits for a user tier (free, pro, internal, etc.)
    Tool-specific limits override the global limit when both are defined.
    """
    tier_name: str
    global_limit: ToolRateLimit
    tool_limits: Dict[str, ToolRateLimit] = field(default_factory=dict)
    suspension_threshold: int = 10      # violations before temporary suspension
    suspension_duration_seconds: float = 300.0

    def limit_for_tool(self, tool_name: str) -> ToolRateLimit:
        return self.tool_limits.get(tool_name, self.global_limit)


def default_policies() -> Dict[str, UserRateLimitPolicy]:
    return {
        "free": UserRateLimitPolicy(
            tier_name="free",
            global_limit=ToolRateLimit(requests_per_window=20, window_seconds=60.0, burst_allowance=5),
            tool_limits={
                "web_search": ToolRateLimit(requests_per_window=5, window_seconds=60.0),
                "llm_call": ToolRateLimit(requests_per_window=10, window_seconds=60.0),
            },
        ),
        "pro": UserRateLimitPolicy(
            tier_name="pro",
            global_limit=ToolRateLimit(requests_per_window=100, window_seconds=60.0, burst_allowance=20),
        ),
        "internal": UserRateLimitPolicy(
            tier_name="internal",
            global_limit=ToolRateLimit(requests_per_window=1000, window_seconds=60.0),
        ),
    }
```

## Solution 2: Sliding Window Rate Limit Counter

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, Tuple


class SlidingWindowCounter:
    """
    Thread-safe sliding window counter for a single (user, tool) key.
    Tracks request timestamps and counts those within the window.
    """

    def __init__(self, window_seconds: float, max_requests: int, burst: int = 0):
        self._window = window_seconds
        self._max = max_requests + burst
        self._timestamps: Deque[float] = deque()
        self._lock = Lock()

    def check_and_record(self) -> Tuple[bool, int, float]:
        """
        Returns (allowed, current_count, retry_after_seconds).
        Records the request if allowed.
        """
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            count = len(self._timestamps)
            if count >= self._max:
                oldest = self._timestamps[0] if self._timestamps else now
                retry_after = max(0.0, oldest + self._window - now)
                return False, count, round(retry_after, 2)
            self._timestamps.append(now)
            return True, count + 1, 0.0

    def current_count(self, now: Optional[float] = None) -> int:
        t = now or time.time()
        cutoff = t - self._window
        with self._lock:
            return sum(1 for ts in self._timestamps if ts >= cutoff)
```

## Solution 3: Per-User Rate Limit Registry

```python
import time
from threading import Lock
from typing import Dict, Optional, Tuple


class PerUserRateLimitRegistry:
    """
    Manages SlidingWindowCounters for (user_id, tool_name) pairs.
    Counters are created lazily and evicted after inactivity.
    """

    def __init__(
        self,
        policies: Dict[str, UserRateLimitPolicy],
        default_tier: str = "free",
        counter_ttl_seconds: float = 600.0,
    ):
        self._policies = policies
        self._default_tier = default_tier
        self._counter_ttl = counter_ttl_seconds
        self._counters: Dict[str, Tuple[SlidingWindowCounter, float]] = {}
        self._user_tiers: Dict[str, str] = {}
        self._violations: Dict[str, int] = {}
        self._suspended_until: Dict[str, float] = {}
        self._lock = Lock()

    def set_user_tier(self, user_id: str, tier: str) -> None:
        with self._lock:
            self._user_tiers[user_id] = tier

    def _get_policy(self, user_id: str) -> UserRateLimitPolicy:
        tier = self._user_tiers.get(user_id, self._default_tier)
        return self._policies.get(tier, self._policies[self._default_tier])

    def _counter_key(self, user_id: str, tool_name: str, scope: RateLimitScope) -> str:
        if scope == RateLimitScope.PER_USER:
            return f"{user_id}:*"
        return f"{user_id}:{tool_name}"

    def check(self, user_id: str, tool_name: str) -> Tuple[bool, dict]:
        now = time.time()

        with self._lock:
            suspended_until = self._suspended_until.get(user_id, 0)
            if suspended_until > now:
                return False, {
                    "allowed": False,
                    "reason": "suspended",
                    "retry_after": round(suspended_until - now, 1),
                }

        policy = self._get_policy(user_id)
        limit = policy.limit_for_tool(tool_name)
        key = self._counter_key(user_id, tool_name, limit.scope)

        with self._lock:
            if key not in self._counters:
                self._counters[key] = (
                    SlidingWindowCounter(limit.window_seconds, limit.requests_per_window, limit.burst_allowance),
                    now,
                )
            counter, _ = self._counters[key]
            self._counters[key] = (counter, now)

        allowed, count, retry_after = counter.check_and_record()

        if not allowed:
            with self._lock:
                self._violations[user_id] = self._violations.get(user_id, 0) + 1
                if self._violations[user_id] >= policy.suspension_threshold:
                    self._suspended_until[user_id] = now + policy.suspension_duration_seconds
                    self._violations[user_id] = 0

        return allowed, {
            "allowed": allowed,
            "user_id": user_id,
            "tool_name": tool_name,
            "current_count": count,
            "limit": limit.requests_per_window,
            "window_seconds": limit.window_seconds,
            "retry_after": retry_after,
            "reason": "rate_limited" if not allowed else "ok",
        }
```

## Solution 4: Rate-Limited Tool Dispatcher

```python
from typing import Any, Callable, Dict, Optional


class RateLimitExceededError(Exception):
    def __init__(self, info: dict):
        super().__init__(
            f"Rate limit exceeded for user '{info.get('user_id')}' "
            f"on tool '{info.get('tool_name')}' — retry after {info.get('retry_after')}s"
        )
        self.info = info


class RateLimitedToolDispatcher:
    """
    Wraps tool dispatch with per-user rate limit enforcement.
    Raises RateLimitExceededError before the tool is called when limit is hit.
    """

    def __init__(
        self,
        registry: PerUserRateLimitRegistry,
        base_dispatch_fn: Callable[[str, Dict[str, Any]], Any],
    ):
        self._registry = registry
        self._dispatch = base_dispatch_fn
        self._blocked_calls = 0
        self._allowed_calls = 0

    async def call(
        self,
        user_id: str,
        tool_name: str,
        args: Dict[str, Any],
    ) -> Any:
        allowed, info = self._registry.check(user_id, tool_name)
        if not allowed:
            self._blocked_calls += 1
            raise RateLimitExceededError(info)
        self._allowed_calls += 1
        return await self._dispatch(tool_name, args)

    def stats(self) -> dict:
        total = self._allowed_calls + self._blocked_calls
        return {
            "allowed_calls": self._allowed_calls,
            "blocked_calls": self._blocked_calls,
            "block_rate": round(self._blocked_calls / total, 3) if total > 0 else 0.0,
        }
```

## Solution 5: Rate Limit Violation Auditor

```python
import json
import time
from pathlib import Path
from threading import Lock
from typing import List


class RateLimitViolationAuditor:
    """
    Persists rate limit violation events for security review.
    High violation rates from a single user indicate abuse patterns.
    """

    def __init__(self, path: str = "/tmp/rate_limit_violations.jsonl"):
        self._path = Path(path)
        self._lock = Lock()

    def record(self, info: dict) -> None:
        event = {
            "ts": time.time(),
            "user_id": info.get("user_id"),
            "tool_name": info.get("tool_name"),
            "reason": info.get("reason"),
            "retry_after": info.get("retry_after"),
        }
        with self._lock:
            with self._path.open("a") as f:
                f.write(json.dumps(event) + "\n")

    def top_violators(self, window_seconds: float = 3600.0, top_n: int = 10) -> List[dict]:
        cutoff = time.time() - window_seconds
        counts: dict = {}
        if not self._path.exists():
            return []
        with self._lock:
            for line in self._path.read_text().splitlines():
                try:
                    e = json.loads(line)
                    if e["ts"] >= cutoff:
                        uid = e.get("user_id", "unknown")
                        counts[uid] = counts.get(uid, 0) + 1
                except (json.JSONDecodeError, KeyError):
                    continue
        return sorted(
            [{"user_id": u, "violations": c} for u, c in counts.items()],
            key=lambda x: -x["violations"],
        )[:top_n]
```

## Solution 6: Rate Limit Dashboard

```python
import time


class PerUserRateLimitDashboard:
    """
    Operational view of rate limiting enforcement, top violators,
    and current suspension list.
    """

    def __init__(
        self,
        registry: PerUserRateLimitRegistry,
        dispatcher: RateLimitedToolDispatcher,
        auditor: RateLimitViolationAuditor,
    ):
        self._registry = registry
        self._dispatcher = dispatcher
        self._auditor = auditor

    def render(self) -> dict:
        with self._registry._lock:
            suspended = {
                uid: round(until - time.time(), 1)
                for uid, until in self._registry._suspended_until.items()
                if until > time.time()
            }
        return {
            "generated_at": time.time(),
            "dispatcher_stats": self._dispatcher.stats(),
            "currently_suspended_users": suspended,
            "top_violators_1h": self._auditor.top_violators(3600.0),
        }
```

## Comparison

| Approach | Per-Tool Limits | Sliding Window | Suspension | Violation Audit | Dashboard |
|---|---|---|---|---|---|
| UserRateLimitPolicy | Yes (tiered) | No | Yes (threshold) | No | No |
| SlidingWindowCounter | No | Yes | No | No | No |
| PerUserRateLimitRegistry | Via policy | Via counter | Yes | No | No |
| RateLimitedToolDispatcher | Via registry | Via registry | Via registry | No | No |
| RateLimitViolationAuditor | No | No | No | Yes (JSONL) | No |
| PerUserRateLimitDashboard | No | No | No | No | Yes |

**Best for production**: Enforce per-tool limits on expensive operations (LLM calls, external API calls) independently from the global per-user limit — a user who makes many cheap tool calls should not be blocked from making a single expensive one. Use graduated enforcement: warn at 80% of the window limit (add a `X-RateLimit-Remaining` header or response field), soft-throttle at 100% (add retry-after delay), hard-block after repeated violations. Set `suspension_threshold=10` violations in 60 seconds to catch automated abuse without penalizing legitimate power users who occasionally burst.
