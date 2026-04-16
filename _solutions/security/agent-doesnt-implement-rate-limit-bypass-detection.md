---
title: "Agent Doesn't Implement Rate Limit Bypass Detection"
description: "Agents with per-IP or per-user rate limits can be bypassed by distributing requests across many accounts, rotating proxies, or exploiting gaps in the rate limit window. Implement rate limit bypass detection that identifies coordinated multi-account patterns, rotating-credential attacks, and window-boundary exploitation, and escalates suspicious traffic to a secondary validation layer before serving."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-rate-limit-bypass-detection
tags: [rate-limit-bypass, coordinated-attack, multi-account, abuse-detection, traffic-analysis, behavioral-fingerprinting]
symptoms:
  - "Per-user rate limits are respected but total system load is 10× normal — distributed across many accounts"
  - "Requests cluster at the rate limit window boundary — possible window-stuffing attack"
  - "Multiple accounts from the same IP subnet sending requests simultaneously"
  - "New accounts created minutes apart show identical usage patterns"
  - "No cross-account visibility — each account's rate limit is enforced in isolation"
---

## Why This Happens

Per-account rate limiting treats each account as an independent entity. A coordinated attack using 100 accounts each sending 10 requests per minute stays under the 10 rpm limit for every individual account while generating 1,000 rpm of total traffic. Detection requires cross-account visibility: grouping accounts by shared attributes (IP, subnet, device fingerprint, behavioral pattern) and applying aggregate limits to the group. Window-boundary exploitation requires sliding windows rather than fixed windows.

## Solution 1: Request Identity Context

```python
from dataclasses import dataclass, field
from typing import FrozenSet, Optional
import time


@dataclass
class RequestIdentityContext:
    user_id: str
    session_id: str
    ip_address: str
    ip_subnet: str          # /24 subnet for grouping
    user_agent: str
    account_age_days: float
    request_id: str
    arrived_at: float = field(default_factory=time.time)
    device_fingerprint: Optional[str] = None
    referrer: Optional[str] = None

    def identity_signals(self) -> FrozenSet[str]:
        """Returns a set of signals for multi-account correlation."""
        signals = {
            f"ip:{self.ip_address}",
            f"subnet:{self.ip_subnet}",
            f"ua:{self.user_agent[:64]}",
        }
        if self.device_fingerprint:
            signals.add(f"fp:{self.device_fingerprint}")
        return frozenset(signals)
```

## Solution 2: Sliding Window Rate Counter

```python
import time
from collections import deque
from typing import Deque, Dict, Optional, Tuple


class SlidingWindowRateCounter:
    """
    Sliding window request counter that avoids fixed-window boundary exploitation.
    Maintains a deque of timestamps and counts requests within the window.
    """

    def __init__(self, window_seconds: float = 60.0) -> None:
        self._window = window_seconds
        self._counters: Dict[str, Deque[float]] = {}

    def _trim(self, key: str) -> None:
        cutoff = time.time() - self._window
        dq = self._counters.get(key)
        if dq:
            while dq and dq[0] < cutoff:
                dq.popleft()

    def increment(self, key: str) -> int:
        """Records a request and returns the current window count."""
        if key not in self._counters:
            self._counters[key] = deque()
        self._trim(key)
        self._counters[key].append(time.time())
        return len(self._counters[key])

    def count(self, key: str) -> int:
        self._trim(key)
        return len(self._counters.get(key, []))

    def rate_per_minute(self, key: str) -> float:
        count = self.count(key)
        return round(count / (self._window / 60.0), 2)

    def burst_at_boundary(self, key: str, boundary_window_seconds: float = 5.0) -> int:
        """Count requests in the last boundary_window_seconds — detects window stuffing."""
        cutoff = time.time() - boundary_window_seconds
        dq = self._counters.get(key, deque())
        return sum(1 for ts in dq if ts >= cutoff)
```

## Solution 3: Multi-Account Correlation Engine

```python
import time
from collections import defaultdict
from typing import Dict, List, Optional, Set


class MultiAccountCorrelationEngine:
    """
    Groups accounts by shared identity signals and tracks aggregate
    request rates per group. Detects when multiple accounts with
    shared signals collectively exceed a threshold.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        group_rate_limit: int = 50,       # max requests/min across the group
        min_accounts_for_group_alert: int = 3,
    ) -> None:
        self._window = window_seconds
        self._group_limit = group_rate_limit
        self._min_accounts = min_accounts_for_group_alert
        self._signal_to_users: Dict[str, Set[str]] = defaultdict(set)
        self._signal_counter = SlidingWindowRateCounter(window_seconds)
        self._user_to_signals: Dict[str, Set[str]] = defaultdict(set)

    def observe(self, ctx: RequestIdentityContext) -> List[dict]:
        """
        Records the request and returns any group-level alerts.
        """
        alerts = []
        for signal in ctx.identity_signals():
            self._signal_to_users[signal].add(ctx.user_id)
            self._user_to_signals[ctx.user_id].add(signal)
            group_count = self._signal_counter.increment(signal)
            account_count = len(self._signal_to_users[signal])

            if (account_count >= self._min_accounts
                    and group_count > self._group_limit):
                alerts.append({
                    "type": "coordinated_multi_account",
                    "signal": signal,
                    "accounts_in_group": account_count,
                    "group_requests_per_min": self._signal_counter.rate_per_minute(signal),
                    "threshold": self._group_limit,
                    "severity": "critical",
                    "triggering_user": ctx.user_id,
                })

        return alerts

    def group_for_user(self, user_id: str) -> Dict[str, int]:
        """Returns all groups the user belongs to with their sizes."""
        result = {}
        for signal in self._user_to_signals.get(user_id, set()):
            result[signal] = len(self._signal_to_users.get(signal, set()))
        return result
```

## Solution 4: Window Boundary Exploit Detector

```python
import time
from collections import defaultdict
from typing import Dict, List


class WindowBoundaryExploitDetector:
    """
    Detects request clustering at rate limit window boundaries,
    which indicates a client deliberately timing requests to maximize
    throughput while staying under per-window limits.
    """

    def __init__(
        self,
        window_seconds: float = 60.0,
        boundary_burst_threshold: int = 8,
        boundary_window_seconds: float = 5.0,
    ) -> None:
        self._window = window_seconds
        self._burst_threshold = boundary_burst_threshold
        self._boundary_window = boundary_window_seconds
        self._per_user_counter = SlidingWindowRateCounter(window_seconds)
        self._boundary_detections: Dict[str, int] = defaultdict(int)

    def observe(self, user_id: str) -> List[dict]:
        self._per_user_counter.increment(user_id)
        alerts = []

        burst = self._per_user_counter.burst_at_boundary(user_id, self._boundary_window)
        if burst >= self._burst_threshold:
            self._boundary_detections[user_id] += 1
            if self._boundary_detections[user_id] >= 3:
                alerts.append({
                    "type": "window_boundary_exploitation",
                    "user_id": user_id,
                    "burst_count": burst,
                    "burst_window_seconds": self._boundary_window,
                    "repeated_detections": self._boundary_detections[user_id],
                    "severity": "warning",
                    "message": (
                        f"User '{user_id}' sent {burst} requests in "
                        f"{self._boundary_window}s window boundary — "
                        "possible rate limit exploitation"
                    ),
                })

        return alerts
```

## Solution 5: New Account Velocity Analyzer

```python
import time
from collections import defaultdict
from typing import Dict, List


class NewAccountVelocityAnalyzer:
    """
    Tracks request rates from newly created accounts.
    High-velocity new accounts with shared signals indicate
    mass account creation for distributed abuse.
    """

    def __init__(
        self,
        new_account_age_threshold_days: float = 7.0,
        new_account_rate_threshold: int = 20,  # requests/min
        cluster_size_alert: int = 5,           # N new accounts from same subnet
    ) -> None:
        self._age_threshold = new_account_age_threshold_days
        self._rate_threshold = new_account_rate_threshold
        self._cluster_size = cluster_size_alert
        self._new_account_counter = SlidingWindowRateCounter(60.0)
        self._subnet_new_accounts: Dict[str, set] = defaultdict(set)

    def observe(self, ctx: RequestIdentityContext) -> List[dict]:
        alerts = []
        if ctx.account_age_days > self._age_threshold:
            return alerts

        # Track new account activity
        rate = self._new_account_counter.increment(f"new:{ctx.user_id}")
        self._subnet_new_accounts[ctx.ip_subnet].add(ctx.user_id)

        subnet_new_count = len(self._subnet_new_accounts[ctx.ip_subnet])
        if subnet_new_count >= self._cluster_size:
            alerts.append({
                "type": "new_account_cluster",
                "subnet": ctx.ip_subnet,
                "new_accounts_in_subnet": subnet_new_count,
                "threshold": self._cluster_size,
                "severity": "warning",
                "message": (
                    f"{subnet_new_count} new accounts (< {self._age_threshold}d old) "
                    f"active from subnet {ctx.ip_subnet}"
                ),
            })

        if rate > self._rate_threshold:
            alerts.append({
                "type": "new_account_high_velocity",
                "user_id": ctx.user_id,
                "rate_per_min": rate,
                "threshold": self._rate_threshold,
                "account_age_days": ctx.account_age_days,
                "severity": "warning",
            })

        return alerts
```

## Solution 6: Bypass Detection Dashboard

```python
import time
from typing import List


class RateLimitBypassDetectionDashboard:
    """
    Aggregates bypass detection signals from all detectors
    into a single abuse operations view.
    """

    def __init__(
        self,
        correlation_engine: MultiAccountCorrelationEngine,
        boundary_detector: WindowBoundaryExploitDetector,
        velocity_analyzer: NewAccountVelocityAnalyzer,
    ) -> None:
        self._correlation = correlation_engine
        self._boundary = boundary_detector
        self._velocity = velocity_analyzer
        self._alert_history: List[dict] = []

    def process(self, ctx: RequestIdentityContext) -> List[dict]:
        """Process a single request through all detectors."""
        all_alerts = []
        all_alerts.extend(self._correlation.observe(ctx))
        all_alerts.extend(self._boundary.observe(ctx.user_id))
        all_alerts.extend(self._velocity.observe(ctx))

        for alert in all_alerts:
            alert["request_id"] = ctx.request_id
            alert["detected_at"] = time.time()
            self._alert_history.append(alert)

        return all_alerts

    def render(self) -> dict:
        recent = [a for a in self._alert_history
                  if time.time() - a.get("detected_at", 0) <= 3600]
        by_type: dict = {}
        for a in recent:
            by_type[a["type"]] = by_type.get(a["type"], 0) + 1

        return {
            "generated_at": time.time(),
            "alerts_last_hour": len(recent),
            "by_type": by_type,
            "critical_count": sum(1 for a in recent if a.get("severity") == "critical"),
        }
```

## Comparison

| Approach | Per-Account Limits | Group Correlation | Boundary Detection | New Account Velocity | Dashboard |
|---|---|---|---|---|---|
| SlidingWindowRateCounter | Yes | No | Yes (burst_at_boundary) | No | No |
| MultiAccountCorrelationEngine | No | Yes (signal grouping) | No | No | No |
| WindowBoundaryExploitDetector | Via counter | No | Yes | No | No |
| NewAccountVelocityAnalyzer | No | No | No | Yes | No |
| RateLimitBypassDetectionDashboard | No | Via engine | Via detector | Via analyzer | Yes |

**Best for production**: Use `/24` subnet grouping as the primary correlation signal — it catches proxy rotation within a datacenter block while being coarse enough to avoid false positives from shared NATs. Apply sliding windows rather than fixed windows for all rate limit counters: fixed windows are trivially exploitable by sending N-1 requests at the end of one window and N-1 at the start of the next. Set `new_account_age_threshold_days=7` — freshly created accounts are the primary vehicle for distributed abuse, and restricting their rate to 20% of normal limits is a low-cost, high-signal defense.
