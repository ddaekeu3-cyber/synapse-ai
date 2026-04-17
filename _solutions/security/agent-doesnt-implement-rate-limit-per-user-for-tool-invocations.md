---
title: "Agent Doesn't Implement Rate Limit Per User for Tool Invocations"
description: "Agents that enforce no per-user rate limit on tool invocations allow a single user to trigger thousands of external API calls, exhaust shared quotas, and generate runaway costs — intentionally or through prompt injection. Implement per-user tool invocation rate limiting with sliding window counters, per-tool overrides, and graceful rejection so one user cannot degrade service for others or trigger unexpected billing spikes."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-rate-limit-per-user-for-tool-invocations
tags: [rate-limiting, per-user, tool-invocation, quota-enforcement, abuse-prevention, cost-control]
symptoms:
  - "Single user triggers thousands of tool calls draining shared API quota"
  - "No per-user ceiling on tool invocations — all users share one global limit"
  - "Prompt injection causes agents to call tools in a tight loop until quota exhausted"
  - "Cost spike traced to one user session with no limit that could have stopped it"
  - "No rejection mechanism when a user exceeds a reasonable tool call rate"
---

## Why This Happens

Most agent implementations gate LLM usage but not tool invocations. Tool calls are often cheaper than LLM tokens individually, so they feel less urgent to limit — until a prompt injection or a misconfigured agent enters a loop, calling a search or database tool hundreds of times per minute. External APIs charge per call regardless of who initiated them, so one unlimited user drains quota shared across all users. Per-user rate limiting requires a sliding window counter keyed by user identity, checked synchronously before each tool dispatch, with a clear rejection signal returned to the agent.

## Solution 1: Rate Limit Policy

```python
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ToolRateLimitPolicy:
    """
    Defines rate limits for tool invocations per user.
    Per-tool overrides take precedence over the global defaults.
    """
    requests_per_minute: int = 60
    requests_per_hour: int = 500
    requests_per_day: int = 5000
    burst_allowance: int = 10            # extra calls allowed briefly above rpm
    per_tool_overrides: Dict[str, "ToolRateLimitPolicy"] = field(default_factory=dict)

    def for_tool(self, tool_name: str) -> "ToolRateLimitPolicy":
        return self.per_tool_overrides.get(tool_name, self)
```

## Solution 2: Sliding Window Rate Limiter

```python
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Deque, Dict, Tuple


class SlidingWindowRateLimiter:
    """
    Per-user sliding window counters for minute, hour, and day windows.
    Thread-safe. Timestamps stored as deques; old entries evicted on check.
    """

    def __init__(self):
        self._lock = Lock()
        # user_key -> window_name -> deque of timestamps
        self._windows: Dict[str, Dict[str, Deque[float]]] = defaultdict(
            lambda: {"minute": deque(), "hour": deque(), "day": deque()}
        )

    def check_and_record(
        self,
        user_key: str,
        policy: ToolRateLimitPolicy,
    ) -> Tuple[bool, str]:
        """
        Returns (allowed, reason).
        Records the call timestamp if allowed.
        """
        now = time.time()
        with self._lock:
            w = self._windows[user_key]

            # Evict stale entries
            self._evict(w["minute"], now - 60)
            self._evict(w["hour"], now - 3600)
            self._evict(w["day"], now - 86400)

            # Check limits
            if len(w["minute"]) >= policy.requests_per_minute + policy.burst_allowance:
                return False, f"rate_limit: {len(w['minute'])} calls in last 60s (limit {policy.requests_per_minute})"
            if len(w["hour"]) >= policy.requests_per_hour:
                return False, f"rate_limit: {len(w['hour'])} calls in last hour (limit {policy.requests_per_hour})"
            if len(w["day"]) >= policy.requests_per_day:
                return False, f"rate_limit: {len(w['day'])} calls today (limit {policy.requests_per_day})"

            # Record
            w["minute"].append(now)
            w["hour"].append(now)
            w["day"].append(now)
            return True, "ok"

    @staticmethod
    def _evict(dq: Deque[float], cutoff: float) -> None:
        while dq and dq[0] < cutoff:
            dq.popleft()

    def current_usage(self, user_key: str) -> dict:
        now = time.time()
        with self._lock:
            w = self._windows.get(user_key, {})
            return {
                "last_minute": sum(1 for t in w.get("minute", []) if t >= now - 60),
                "last_hour": sum(1 for t in w.get("hour", []) if t >= now - 3600),
                "last_day": sum(1 for t in w.get("day", []) if t >= now - 86400),
            }
```

## Solution 3: Per-User Policy Registry

```python
from typing import Dict, Optional


DEFAULT_POLICY = ToolRateLimitPolicy(
    requests_per_minute=60,
    requests_per_hour=500,
    requests_per_day=5000,
)

ELEVATED_POLICY = ToolRateLimitPolicy(
    requests_per_minute=200,
    requests_per_hour=2000,
    requests_per_day=20000,
)


class UserRateLimitPolicyRegistry:
    """
    Maps user identifiers to rate limit policies.
    Falls back to the global default when no per-user policy is set.
    """

    def __init__(self, default_policy: Optional[ToolRateLimitPolicy] = None):
        self._default = default_policy or DEFAULT_POLICY
        self._policies: Dict[str, ToolRateLimitPolicy] = {}

    def set_policy(self, user_id: str, policy: ToolRateLimitPolicy) -> None:
        self._policies[user_id] = policy

    def remove_policy(self, user_id: str) -> None:
        self._policies.pop(user_id, None)

    def get(self, user_id: str) -> ToolRateLimitPolicy:
        return self._policies.get(user_id, self._default)
```

## Solution 4: Rate-Limited Tool Dispatcher

```python
import time
from typing import Any, Callable, Optional


class RateLimitExceededError(Exception):
    def __init__(self, user_id: str, tool_name: str, reason: str):
        super().__init__(
            f"Rate limit exceeded for user '{user_id}' on tool '{tool_name}': {reason}"
        )
        self.user_id = user_id
        self.tool_name = tool_name
        self.reason = reason


class RateLimitedToolDispatcher:
    """
    Enforces per-user, per-tool rate limits before dispatching tool calls.
    Raises RateLimitExceededError when a user exceeds their policy ceiling.
    """

    def __init__(
        self,
        limiter: SlidingWindowRateLimiter,
        policy_registry: UserRateLimitPolicyRegistry,
    ):
        self._limiter = limiter
        self._registry = policy_registry
        self._rejections: list = []

    async def dispatch(
        self,
        user_id: str,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        policy = self._registry.get(user_id).for_tool(tool_name)
        user_tool_key = f"{user_id}::{tool_name}"

        allowed, reason = self._limiter.check_and_record(user_tool_key, policy)
        if not allowed:
            self._rejections.append({
                "ts": time.time(),
                "user_id": user_id,
                "tool_name": tool_name,
                "reason": reason,
            })
            raise RateLimitExceededError(user_id, tool_name, reason)

        return await tool_fn(*args, **kwargs)

    def recent_rejections(self, last_n: int = 100) -> list:
        return self._rejections[-last_n:]
```

## Solution 5: Abuse Pattern Detector

```python
import time
from collections import defaultdict
from threading import Lock
from typing import Dict, List


class ToolAbusePatternDetector:
    """
    Detects users whose rejection rate suggests systematic abuse
    (prompt injection loops, automated scraping, runaway agents).
    """

    def __init__(self, detection_window_seconds: float = 300.0):
        self._lock = Lock()
        self._rejections: Dict[str, List[float]] = defaultdict(list)
        self._window = detection_window_seconds

    def record_rejection(self, user_id: str) -> None:
        with self._lock:
            now = time.time()
            self._rejections[user_id].append(now)
            cutoff = now - self._window
            self._rejections[user_id] = [
                t for t in self._rejections[user_id] if t >= cutoff
            ]

    def high_rejection_users(self, threshold: int = 10) -> List[dict]:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            result = []
            for user_id, timestamps in self._rejections.items():
                recent = [t for t in timestamps if t >= cutoff]
                if len(recent) >= threshold:
                    result.append({
                        "user_id": user_id,
                        "rejections_in_window": len(recent),
                        "window_seconds": self._window,
                        "recommendation": "investigate for prompt injection or abuse",
                    })
        return sorted(result, key=lambda x: -x["rejections_in_window"])
```

## Solution 6: Rate Limit Audit Logger

```python
import time
from typing import List


class ToolRateLimitAuditLogger:
    """
    Records rate limit events (rejections and near-limit warnings)
    for compliance reporting and capacity planning.
    """

    def __init__(self, near_limit_pct: float = 0.80, max_records: int = 10000):
        self._near_pct = near_limit_pct
        self._max = max_records
        self._records: List[dict] = []

    def log_rejection(
        self, user_id: str, tool_name: str, reason: str
    ) -> None:
        self._append({
            "event": "rate_limit_rejection",
            "user_id": user_id,
            "tool_name": tool_name,
            "reason": reason,
        })

    def log_near_limit(
        self, user_id: str, tool_name: str, usage: dict, policy: ToolRateLimitPolicy
    ) -> None:
        utilization = usage["last_minute"] / max(policy.requests_per_minute, 1)
        if utilization >= self._near_pct:
            self._append({
                "event": "rate_limit_warning",
                "user_id": user_id,
                "tool_name": tool_name,
                "utilization_pct": round(utilization * 100, 1),
            })

    def _append(self, record: dict) -> None:
        record["ts"] = time.time()
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append(record)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        rejections = [r for r in recent if r["event"] == "rate_limit_rejection"]
        unique_users = {r["user_id"] for r in rejections}
        return {
            "window_seconds": window_seconds,
            "total_rejections": len(rejections),
            "unique_users_rejected": len(unique_users),
            "top_rejected_users": self._top_users(rejections, n=5),
        }

    @staticmethod
    def _top_users(records: list, n: int) -> List[dict]:
        counts: Dict[str, int] = defaultdict(int)
        for r in records:
            counts[r["user_id"]] += 1
        return sorted(
            [{"user_id": u, "rejections": c} for u, c in counts.items()],
            key=lambda x: -x["rejections"],
        )[:n]
```

## Comparison

| Approach | Sliding Window | Per-Tool Override | Policy Registry | Abuse Detection | Audit Log |
|---|---|---|---|---|---|
| SlidingWindowRateLimiter | Yes (min/hr/day) | No | No | No | No |
| UserRateLimitPolicyRegistry | No | Via policy | Yes | No | No |
| RateLimitedToolDispatcher | Via limiter | Via registry | Via registry | No | No |
| ToolAbusePatternDetector | No | No | No | Yes (rejection rate) | No |
| ToolRateLimitAuditLogger | No | No | No | No | Yes |

**Best for production**: Key the sliding window on `user_id::tool_name` rather than `user_id` alone so a user hitting a search tool limit does not block them from calling a read-only lookup tool. Set per-tool overrides for expensive external APIs — a web search tool might be limited to 10 calls/minute while an internal database lookup allows 200. Run `ToolAbusePatternDetector.high_rejection_users()` as a scheduled check every five minutes: three or more users with 10+ rejections in a five-minute window indicates a systemic issue (prompt injection campaign or runaway agent) that warrants investigation beyond rate limiting alone.
