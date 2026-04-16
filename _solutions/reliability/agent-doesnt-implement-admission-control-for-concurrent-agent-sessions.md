---
title: "Agent Doesn't Implement Admission Control for Concurrent Agent Sessions"
description: "Agents that accept every incoming session without limits allow concurrent load to pile up unbounded: memory exhaustion, model rate-limit saturation, and degraded latency for all active sessions. Implement admission control to cap concurrent sessions, queue overflow requests with priority ordering, reject beyond a queue depth threshold, and expose wait-time estimates — keeping the active fleet within safe operating limits."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-admission-control-for-concurrent-agent-sessions
tags: [admission-control, concurrency, session-management, queue-management, overload-protection, rate-limiting]
symptoms:
  - "Memory usage grows linearly with incoming request rate — no upper bound"
  - "Model API rate limits hit because too many sessions call the API simultaneously"
  - "P99 latency spikes during traffic bursts even for sessions that started first"
  - "No way to tell callers how long they will wait before a session slot opens"
  - "Graceful shutdown takes minutes because hundreds of sessions are in flight"
---

## Why This Happens

Agent runtimes start a new session for every incoming request without checking whether the system has capacity. At low load this is fine; during traffic spikes it causes resource saturation. The fix is an admission gate at session creation time: maintain an active-session counter, queue requests that arrive when the counter is at maximum, reject requests when the queue is also full, and release a slot when any session terminates so the next queued request can proceed. Priority ordering ensures high-priority sessions (retries, premium users) are admitted before low-priority ones.

## Solution 1: Session Admission Gate

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class AdmissionDecision(str, Enum):
    ADMITTED = "admitted"
    QUEUED = "queued"
    REJECTED = "rejected"


@dataclass
class AdmissionRequest:
    session_id: str
    priority: int = 0          # higher = more important
    requested_at: float = field(default_factory=time.time)
    timeout_seconds: float = 30.0
    metadata: dict = field(default_factory=dict)

    def is_expired(self) -> bool:
        return time.time() - self.requested_at > self.timeout_seconds


@dataclass
class AdmissionResult:
    decision: AdmissionDecision
    session_id: str
    wait_seconds: float = 0.0
    queue_position: int = 0
    reason: str = ""
```

## Solution 2: Concurrent Session Limiter

```python
import asyncio
import heapq
import time
from typing import Dict, List, Optional, Tuple


class ConcurrentSessionLimiter:
    """
    Enforces a hard cap on simultaneously active agent sessions.
    Requests beyond the cap are queued by priority and wait for a free slot.
    Requests beyond max_queue_depth are rejected immediately.
    """

    def __init__(
        self,
        max_concurrent: int = 50,
        max_queue_depth: int = 200,
        default_timeout_seconds: float = 30.0,
    ):
        self._max_concurrent = max_concurrent
        self._max_queue = max_queue_depth
        self._default_timeout = default_timeout_seconds
        self._active: Dict[str, float] = {}          # session_id -> admitted_at
        self._queue: List[Tuple] = []                # min-heap: (-priority, seq, future, req)
        self._seq = 0
        self._lock = asyncio.Lock()
        self._slot_available = asyncio.Event()
        self._total_admitted = 0
        self._total_rejected = 0
        self._total_queued = 0

    async def request_admission(
        self, request: AdmissionRequest
    ) -> AdmissionResult:
        async with self._lock:
            # Slot available — admit immediately
            if len(self._active) < self._max_concurrent:
                self._active[request.session_id] = time.time()
                self._total_admitted += 1
                return AdmissionResult(
                    decision=AdmissionDecision.ADMITTED,
                    session_id=request.session_id,
                )

            # Queue full — reject
            if len(self._queue) >= self._max_queue:
                self._total_rejected += 1
                return AdmissionResult(
                    decision=AdmissionDecision.REJECTED,
                    session_id=request.session_id,
                    reason="queue_full",
                )

            # Queue the request
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            self._seq += 1
            # Negate priority for min-heap (higher priority = lower heap key)
            heapq.heappush(
                self._queue,
                (-request.priority, self._seq, future, request),
            )
            position = len(self._queue)
            self._total_queued += 1

        # Wait outside the lock
        wait_estimate = self._estimate_wait(position)
        try:
            await asyncio.wait_for(future, timeout=request.timeout_seconds)
            return AdmissionResult(
                decision=AdmissionDecision.ADMITTED,
                session_id=request.session_id,
                wait_seconds=time.time() - request.requested_at,
                queue_position=position,
            )
        except asyncio.TimeoutError:
            async with self._lock:
                self._queue = [
                    item for item in self._queue if item[2] is not future
                ]
                heapq.heapify(self._queue)
            return AdmissionResult(
                decision=AdmissionDecision.REJECTED,
                session_id=request.session_id,
                reason="queue_timeout",
                wait_seconds=request.timeout_seconds,
            )

    async def release(self, session_id: str) -> None:
        async with self._lock:
            self._active.pop(session_id, None)
            self._promote_next()

    def _promote_next(self) -> None:
        """Admit the highest-priority queued request (called while lock held)."""
        while self._queue and len(self._active) < self._max_concurrent:
            _, _, future, req = heapq.heappop(self._queue)
            if req.is_expired() or future.done():
                continue
            self._active[req.session_id] = time.time()
            self._total_admitted += 1
            if not future.done():
                future.set_result(True)

    def _estimate_wait(self, queue_position: int) -> float:
        if not self._active:
            return 0.0
        oldest = min(self._active.values())
        avg_session_age = (time.time() - oldest) / max(len(self._active), 1)
        return avg_session_age * queue_position / max(self._max_concurrent, 1)

    def stats(self) -> dict:
        return {
            "active_sessions": len(self._active),
            "queued_requests": len(self._queue),
            "max_concurrent": self._max_concurrent,
            "max_queue_depth": self._max_queue,
            "utilization": round(len(self._active) / self._max_concurrent, 3),
            "total_admitted": self._total_admitted,
            "total_queued": self._total_queued,
            "total_rejected": self._total_rejected,
        }
```

## Solution 3: Priority-Aware Admission Policy

```python
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional


@dataclass
class AdmissionPolicy:
    """
    Assigns priority and timeout to incoming session requests.
    Retry requests get higher priority than first attempts.
    Premium users get higher priority than free users.
    """
    base_priority: int = 0
    retry_priority_boost: int = 10
    premium_priority_boost: int = 5
    timeout_seconds: float = 30.0


class PriorityAdmissionPolicy:
    """
    Computes admission priority from session metadata.
    Higher priority → admitted sooner when slots are scarce.
    """

    def __init__(self):
        self._tier_priorities: Dict[str, int] = {
            "enterprise": 20,
            "premium": 10,
            "standard": 0,
            "free": -5,
        }
        self._tier_timeouts: Dict[str, float] = {
            "enterprise": 60.0,
            "premium": 45.0,
            "standard": 30.0,
            "free": 15.0,
        }

    def evaluate(
        self,
        session_id: str,
        user_tier: str = "standard",
        is_retry: bool = False,
        attempt_number: int = 1,
    ) -> AdmissionRequest:
        base = self._tier_priorities.get(user_tier, 0)
        retry_boost = min(attempt_number * 5, 20) if is_retry else 0
        timeout = self._tier_timeouts.get(user_tier, 30.0)

        return AdmissionRequest(
            session_id=session_id,
            priority=base + retry_boost,
            timeout_seconds=timeout,
            metadata={
                "user_tier": user_tier,
                "is_retry": is_retry,
                "attempt_number": attempt_number,
            },
        )
```

## Solution 4: Admission Control Context Manager

```python
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator


class AdmissionControlledSessionManager:
    """
    Context manager that acquires admission before yielding a session slot
    and releases it when the session exits — even on exceptions.
    """

    def __init__(
        self,
        limiter: ConcurrentSessionLimiter,
        policy: PriorityAdmissionPolicy,
    ):
        self._limiter = limiter
        self._policy = policy

    @asynccontextmanager
    async def session(
        self,
        session_id: str,
        user_tier: str = "standard",
        is_retry: bool = False,
    ) -> AsyncIterator[AdmissionResult]:
        request = self._policy.evaluate(
            session_id=session_id,
            user_tier=user_tier,
            is_retry=is_retry,
        )
        result = await self._limiter.request_admission(request)
        if result.decision == AdmissionDecision.REJECTED:
            raise RuntimeError(
                f"session {session_id} rejected: {result.reason}"
            )
        try:
            yield result
        finally:
            await self._limiter.release(session_id)
```

## Solution 5: Admission Rate Tracker

```python
import time
from collections import deque
from typing import Deque, List


class AdmissionRateTracker:
    """
    Tracks admission, queue, and rejection rates over a sliding window.
    Used to auto-tune max_concurrent and detect admission pressure.
    """

    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        self._admitted: Deque[float] = deque()
        self._queued: Deque[float] = deque()
        self._rejected: Deque[float] = deque()

    def record_admitted(self) -> None:
        self._admitted.append(time.time())

    def record_queued(self) -> None:
        self._queued.append(time.time())

    def record_rejected(self) -> None:
        self._rejected.append(time.time())

    def _trim(self, q: Deque[float]) -> None:
        cutoff = time.time() - self._window
        while q and q[0] < cutoff:
            q.popleft()

    def rates(self) -> dict:
        for q in (self._admitted, self._queued, self._rejected):
            self._trim(q)
        total = len(self._admitted) + len(self._queued) + len(self._rejected)
        return {
            "window_seconds": self._window,
            "admitted_per_min": round(len(self._admitted) / self._window * 60, 1),
            "queued_per_min": round(len(self._queued) / self._window * 60, 1),
            "rejected_per_min": round(len(self._rejected) / self._window * 60, 1),
            "rejection_rate": round(len(self._rejected) / max(total, 1), 4),
            "queue_pressure": round(len(self._queued) / max(total, 1), 4),
        }
```

## Solution 6: Admission Control Dashboard

```python
import time


class AdmissionControlDashboard:
    """
    Aggregates admission limiter stats and rate tracker into an operational view.
    Emits alerts when rejection rate or queue depth exceeds thresholds.
    """

    def __init__(
        self,
        limiter: ConcurrentSessionLimiter,
        rate_tracker: AdmissionRateTracker,
        max_rejection_rate: float = 0.05,
        max_queue_utilization: float = 0.80,
    ):
        self._limiter = limiter
        self._tracker = rate_tracker
        self._max_rejection_rate = max_rejection_rate
        self._max_queue_util = max_queue_utilization

    def render(self) -> dict:
        stats = self._limiter.stats()
        rates = self._tracker.rates()
        queue_util = stats["queued_requests"] / max(
            self._limiter._max_queue, 1
        )

        alerts = []
        if rates["rejection_rate"] > self._max_rejection_rate:
            alerts.append({
                "type": "high_rejection_rate",
                "value": rates["rejection_rate"],
                "threshold": self._max_rejection_rate,
                "recommendation": "increase max_concurrent or add capacity",
            })
        if queue_util > self._max_queue_util:
            alerts.append({
                "type": "queue_near_full",
                "utilization": round(queue_util, 3),
                "threshold": self._max_queue_util,
                "recommendation": "reduce admission timeout or increase queue depth",
            })

        return {
            "generated_at": time.time(),
            "capacity": {
                "active_sessions": stats["active_sessions"],
                "max_concurrent": stats["max_concurrent"],
                "utilization": stats["utilization"],
            },
            "queue": {
                "depth": stats["queued_requests"],
                "max_depth": self._limiter._max_queue,
                "utilization": round(queue_util, 3),
            },
            "rates": rates,
            "totals": {
                "admitted": stats["total_admitted"],
                "queued": stats["total_queued"],
                "rejected": stats["total_rejected"],
            },
            "alerts": alerts,
            "healthy": len(alerts) == 0,
        }
```

## Comparison

| Approach | Concurrency Cap | Priority Queue | Wait Estimate | Metrics |
|---|---|---|---|---|
| ConcurrentSessionLimiter | Yes (hard cap) | Yes (min-heap) | Yes (heuristic) | Yes |
| PriorityAdmissionPolicy | No | Yes (tier + retry boost) | No | No |
| AdmissionControlledSessionManager | Via limiter | Via policy | No | No |
| AdmissionRateTracker | No | No | No | Yes (sliding window) |
| AdmissionControlDashboard | No | No | No | Yes (aggregated) |

**Best for production**: Set `max_concurrent` to the number of concurrent model API calls your rate limits allow — not to machine RAM. Use `PriorityAdmissionPolicy` with enterprise/premium/standard/free tiers so bursts from low-priority users don't crowd out paying customers. Wrap every session with `AdmissionControlledSessionManager` so slots are always released on error. Monitor `AdmissionControlDashboard.render()` every 30 seconds — a `rejection_rate` above 5% means you need to scale out or tighten upstream traffic.
