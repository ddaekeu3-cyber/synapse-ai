---
title: "Agent Doesn't Implement Resource Reservation Before Long-Running Tasks"
description: "Agents that start multi-step tasks without pre-reserving required resources — API quota, database connections, token budgets, downstream capacity — fail halfway through after partial state mutations. Implement upfront resource reservation to validate availability before committing to a task, with automatic release on completion or timeout."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-resource-reservation-before-long-running-task
tags: [resource-reservation, capacity-checking, reliability, multi-step-tasks, quota-management, pre-flight]
symptoms:
  - "Agent completes 8 of 10 tool calls then hits API rate limit — leaves partial results"
  - "Long-running task starts successfully but fails when database connection pool is exhausted"
  - "Token budget exhausted at step 7 of a 10-step workflow requiring compensation"
  - "No pre-flight check — agent discovers missing permissions only after side effects are made"
  - "Resource exhaustion mid-task causes inconsistent state requiring manual cleanup"
---

## Why This Happens

Multi-step agent tasks are expensive to roll back. Once a tool call modifies external state (sends an email, writes a database record, charges a payment), subsequent failures require compensating transactions. A pre-flight resource reservation checks that all required resources are available before any state mutation — if any check fails, the task is refused cleanly before anything is changed.

## Solution 1: Resource Reservation Manager

```python
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class ResourceRequirement:
    resource_type: str     # "api_quota" | "db_connections" | "token_budget" | "memory_mb"
    amount: float
    resource_key: str = ""   # specific key within resource type (e.g., "openai:gpt-4")

@dataclass
class Reservation:
    reservation_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    task_id: str = ""
    requirements: List[ResourceRequirement] = field(default_factory=list)
    reserved_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    released: bool = False
    used: Dict[str, float] = field(default_factory=dict)

class ResourceReservationManager:
    """
    Manages pre-flight resource reservations for long-running tasks.
    Checks availability before reserving; releases on completion or TTL expiry.
    Prevents multiple tasks from over-committing the same resource pool.
    """

    def __init__(self):
        self._capacity: Dict[str, float] = {}       # resource_key -> total capacity
        self._reserved: Dict[str, float] = {}       # resource_key -> currently reserved
        self._reservations: Dict[str, Reservation] = {}
        self._lock = asyncio.Lock()

    def register_resource(self, resource_key: str, total_capacity: float) -> None:
        self._capacity[resource_key] = total_capacity
        self._reserved[resource_key] = 0.0

    def available(self, resource_key: str) -> float:
        capacity = self._capacity.get(resource_key, float("inf"))
        reserved = self._reserved.get(resource_key, 0.0)
        return max(0.0, capacity - reserved)

    async def reserve(
        self,
        task_id: str,
        requirements: List[ResourceRequirement],
        ttl_seconds: float = 300.0,
    ) -> tuple[bool, Optional[Reservation], List[str]]:
        """
        Returns (success, reservation, failed_resources).
        On failure, returns (False, None, list_of_failed_checks).
        """
        async with self._lock:
            failed = []
            for req in requirements:
                key = req.resource_key or req.resource_type
                if self.available(key) < req.amount:
                    failed.append(
                        f"{key}: need {req.amount}, available {self.available(key):.1f}"
                    )

            if failed:
                return False, None, failed

            reservation = Reservation(
                task_id=task_id,
                requirements=requirements,
                expires_at=time.time() + ttl_seconds,
            )
            for req in requirements:
                key = req.resource_key or req.resource_type
                self._reserved[key] = self._reserved.get(key, 0.0) + req.amount

            self._reservations[reservation.reservation_id] = reservation
            return True, reservation, []

    async def release(self, reservation_id: str) -> bool:
        async with self._lock:
            reservation = self._reservations.get(reservation_id)
            if not reservation or reservation.released:
                return False
            for req in reservation.requirements:
                key = req.resource_key or req.resource_type
                self._reserved[key] = max(
                    0.0, self._reserved.get(key, 0.0) - req.amount
                )
            reservation.released = True
            return True

    async def expire_stale(self) -> int:
        """Release reservations past their TTL."""
        now = time.time()
        expired = [
            rid for rid, r in self._reservations.items()
            if r.expires_at < now and not r.released
        ]
        for rid in expired:
            await self.release(rid)
        return len(expired)

    def utilization(self) -> Dict[str, float]:
        result = {}
        for key, capacity in self._capacity.items():
            reserved = self._reserved.get(key, 0.0)
            result[key] = round(reserved / max(capacity, 1e-9), 3)
        return result
```

## Solution 2: Pre-Flight Checker

```python
import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Dict, List, Optional

@dataclass
class PreFlightCheck:
    check_id: str
    description: str
    check_fn: Callable[[], Coroutine]   # returns (passed: bool, reason: str)
    blocking: bool = True               # if True, failure prevents task start

@dataclass
class PreFlightResult:
    all_passed: bool
    passed: List[str]
    failed: List[dict]
    warnings: List[dict]

class PreFlightChecker:
    """
    Runs a set of checks before a task starts.
    Blocking checks must pass; non-blocking checks produce warnings.
    """

    def __init__(self):
        self._checks: List[PreFlightCheck] = []

    def register(self, check: PreFlightCheck) -> None:
        self._checks.append(check)

    async def run(self, checks: Optional[List[PreFlightCheck]] = None) -> PreFlightResult:
        targets = checks or self._checks
        results = await asyncio.gather(
            *[self._run_check(c) for c in targets],
            return_exceptions=False,
        )
        passed, failed, warnings = [], [], []
        for check, (ok, reason) in zip(targets, results):
            if ok:
                passed.append(check.check_id)
            elif check.blocking:
                failed.append({"check_id": check.check_id, "reason": reason})
            else:
                warnings.append({"check_id": check.check_id, "reason": reason})

        return PreFlightResult(
            all_passed=len(failed) == 0,
            passed=passed,
            failed=failed,
            warnings=warnings,
        )

    async def _run_check(self, check: PreFlightCheck) -> tuple:
        try:
            result = await check.check_fn()
            if isinstance(result, tuple):
                return result
            return (bool(result), "")
        except Exception as exc:
            return (False, str(exc))
```

## Solution 3: Token Budget Reservation

```python
import time
from dataclasses import dataclass, field
from typing import Dict, Optional

@dataclass
class TokenBudgetReservation:
    reservation_id: str
    session_id: str
    reserved_tokens: int
    used_tokens: int = 0
    reserved_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    released: bool = False

    @property
    def remaining(self) -> int:
        return self.reserved_tokens - self.used_tokens

class TokenBudgetReserver:
    """
    Reserves token budget before starting a task.
    Tracks consumption against the reservation to detect budget overruns.
    """

    def __init__(
        self,
        session_token_limit: int,
        safety_margin: float = 0.9,
    ):
        self._limit = session_token_limit
        self._margin = safety_margin
        self._committed: int = 0
        self._reservations: Dict[str, TokenBudgetReservation] = {}

    def available_to_reserve(self) -> int:
        return int(self._limit * self._margin) - self._committed

    def reserve(
        self,
        reservation_id: str,
        session_id: str,
        estimated_tokens: int,
        ttl_seconds: float = 600.0,
    ) -> tuple[bool, Optional[TokenBudgetReservation]]:
        if estimated_tokens > self.available_to_reserve():
            return False, None

        reservation = TokenBudgetReservation(
            reservation_id=reservation_id,
            session_id=session_id,
            reserved_tokens=estimated_tokens,
            expires_at=time.time() + ttl_seconds,
        )
        self._committed += estimated_tokens
        self._reservations[reservation_id] = reservation
        return True, reservation

    def consume(self, reservation_id: str, tokens: int) -> bool:
        r = self._reservations.get(reservation_id)
        if not r or r.released:
            return False
        if tokens > r.remaining:
            return False   # would exceed reservation
        r.used_tokens += tokens
        return True

    def release(self, reservation_id: str) -> None:
        r = self._reservations.get(reservation_id)
        if r and not r.released:
            self._committed -= r.reserved_tokens
            r.released = True

    def stats(self) -> dict:
        active = [r for r in self._reservations.values() if not r.released]
        return {
            "limit": self._limit,
            "committed": self._committed,
            "available": self.available_to_reserve(),
            "active_reservations": len(active),
        }
```

## Solution 4: Task Admission Controller

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

@dataclass
class TaskDescriptor:
    task_id: str
    task_type: str
    estimated_steps: int
    estimated_tokens: int
    resource_requirements: List[ResourceRequirement]
    priority: int = 1
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class AdmissionDecision:
    admitted: bool
    task_id: str
    reservation_id: Optional[str]
    failed_checks: List[str]
    failed_resources: List[str]
    reason: str

class TaskAdmissionController:
    """
    Single entry point for task admission.
    Runs pre-flight checks and reserves resources atomically.
    Refuses tasks that cannot be completed given current resource state.
    """

    def __init__(
        self,
        reservation_manager: ResourceReservationManager,
        preflight_checker: PreFlightChecker,
        token_reserver: TokenBudgetReserver,
    ):
        self._reservations = reservation_manager
        self._preflight = preflight_checker
        self._tokens = token_reserver
        self._admitted = 0
        self._rejected = 0

    async def admit(
        self,
        task: TaskDescriptor,
        extra_checks: Optional[List[PreFlightCheck]] = None,
    ) -> AdmissionDecision:
        failed_checks = []
        failed_resources = []

        # Run pre-flight checks
        preflight_result = await self._preflight.run(extra_checks)
        if not preflight_result.all_passed:
            failed_checks = [f["check_id"] for f in preflight_result.failed]

        # Reserve token budget
        reserved_tokens = False
        if task.estimated_tokens > 0:
            ok, token_res = self._tokens.reserve(
                task.task_id, task.task_id, task.estimated_tokens
            )
            reserved_tokens = ok
            if not ok:
                failed_resources.append(
                    f"token_budget: need {task.estimated_tokens}, "
                    f"available {self._tokens.available_to_reserve()}"
                )

        # Reserve other resources
        resource_ok, reservation, resource_failures = await self._reservations.reserve(
            task.task_id, task.resource_requirements
        )
        if not resource_ok:
            failed_resources.extend(resource_failures)

        all_ok = (not failed_checks and not failed_resources)
        if all_ok:
            self._admitted += 1
        else:
            self._rejected += 1
            # Release token budget if resource reservation failed
            if reserved_tokens and not resource_ok:
                self._tokens.release(task.task_id)

        return AdmissionDecision(
            admitted=all_ok,
            task_id=task.task_id,
            reservation_id=reservation.reservation_id if (reservation and all_ok) else None,
            failed_checks=failed_checks,
            failed_resources=failed_resources,
            reason="ok" if all_ok else "pre_flight_failed",
        )

    def stats(self) -> dict:
        return {
            "admitted": self._admitted,
            "rejected": self._rejected,
            "admission_rate": round(
                self._admitted / max(self._admitted + self._rejected, 1), 3
            ),
            "resource_utilization": self._reservations.utilization(),
            "token_stats": self._tokens.stats(),
        }
```

## Solution 5: Reservation Lease Renewer

```python
import asyncio
import time

class ReservationLeaseRenewer:
    """
    Periodically renews reservation TTLs for long-running tasks.
    Prevents expiry from releasing resources while the task is still active.
    """

    def __init__(
        self,
        manager: ResourceReservationManager,
        renew_interval_seconds: float = 60.0,
        extend_by_seconds: float = 300.0,
    ):
        self._manager = manager
        self._interval = renew_interval_seconds
        self._extend = extend_by_seconds
        self._active: dict = {}   # reservation_id -> True

    def track(self, reservation_id: str) -> None:
        self._active[reservation_id] = True

    def untrack(self, reservation_id: str) -> None:
        self._active.pop(reservation_id, None)

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            now = time.time()
            async with self._manager._lock:
                for rid in list(self._active):
                    reservation = self._manager._reservations.get(rid)
                    if reservation and not reservation.released:
                        reservation.expires_at = now + self._extend
```

## Solution 6: Admission Metrics Dashboard

```python
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict

class AdmissionMetricsDashboard:
    def __init__(self):
        self._decisions: Deque[AdmissionDecision] = deque(maxlen=1000)
        self._rejection_reasons: Dict[str, int] = defaultdict(int)

    def record(self, decision: AdmissionDecision) -> None:
        self._decisions.append(decision)
        if not decision.admitted:
            for check in decision.failed_checks:
                self._rejection_reasons[f"check:{check}"] += 1
            for resource in decision.failed_resources:
                self._rejection_reasons[f"resource:{resource.split(':')[0]}"] += 1

    def summary(self) -> dict:
        total = len(self._decisions)
        admitted = sum(1 for d in self._decisions if d.admitted)
        return {
            "total_requests": total,
            "admitted": admitted,
            "rejected": total - admitted,
            "admission_rate": round(admitted / max(total, 1), 3),
            "top_rejection_reasons": sorted(
                self._rejection_reasons.items(),
                key=lambda x: x[1], reverse=True,
            )[:5],
        }
```

## Comparison

| Approach | Resource Tracking | Pre-flight Checks | Token Budget | Lease Renewal |
|---|---|---|---|---|
| ResourceReservationManager | Yes | No | No | No |
| PreFlightChecker | No | Yes | No | No |
| TokenBudgetReserver | No | No | Yes | No |
| TaskAdmissionController | Yes (combined) | Yes | Yes | No |
| ReservationLeaseRenewer | No | No | No | Yes |
| AdmissionMetricsDashboard | N/A | N/A | N/A | N/A |

**Best for production**: Gate all multi-step task starts through `TaskAdmissionController`. Register resource capacities (API rate limits, DB connections, token budgets) in `ResourceReservationManager`. Add domain-specific `PreFlightCheck` instances for permissions, feature flags, and downstream health. Start `ReservationLeaseRenewer` as a background task for any task expected to run longer than the TTL. Track rejection rates in `AdmissionMetricsDashboard` to identify capacity bottlenecks before they cascade into user-visible failures.
