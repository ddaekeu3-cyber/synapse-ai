---
title: "Agent Doesn't Implement Fan-Out Fan-In for Parallel Subtask Aggregation"
description: "Agents that execute multi-step workflows sequentially miss parallelism opportunities and can't recover gracefully when one subtask fails. Implement fan-out fan-in to decompose work into parallel subtasks, aggregate results with configurable partial-failure tolerance, and enforce a unified timeout across all branches."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-fan-out-fan-in-for-parallel-subtask-aggregation
tags: [fan-out, fan-in, parallel-execution, subtask-aggregation, partial-failure, reliability]
symptoms:
  - "Agent runs 5 independent retrieval calls sequentially, taking 5× longer than necessary"
  - "One failing subtask cancels all parallel work even though 4 of 5 results are usable"
  - "No timeout coordination across parallel branches — slow branch blocks the whole response"
  - "Partial results discarded on any single failure instead of continuing with available data"
  - "Fan-out implemented with asyncio.gather but missing exception handling and result merging"
---

## Why This Happens

`asyncio.gather` is the common starting point for parallel execution but lacks partial-failure policies, per-branch timeouts, result weighting, and aggregation strategies. A single exception in `gather` (with default settings) propagates and discards all other results. Production fan-out patterns need configurable minimum-success thresholds, branch-level timeouts, and pluggable aggregators that can handle heterogeneous result types.

## Solution 1: Fan-Out Executor with Partial Failure Policy

```python
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine, List, Optional

class FailurePolicy(str, Enum):
    REQUIRE_ALL = "require_all"       # fail if any branch fails
    REQUIRE_MAJORITY = "require_majority"  # fail if < 50% succeed
    REQUIRE_N = "require_n"           # fail if < N succeed
    BEST_EFFORT = "best_effort"       # return whatever succeeds

@dataclass
class BranchResult:
    branch_id: str
    success: bool
    value: Any = None
    error: Optional[Exception] = None
    duration_ms: float = 0.0

@dataclass
class FanOutResult:
    succeeded: List[BranchResult]
    failed: List[BranchResult]
    total_duration_ms: float
    policy_satisfied: bool

    @property
    def values(self) -> List[Any]:
        return [r.value for r in self.succeeded]

class FanOutExecutor:
    """
    Executes multiple coroutines in parallel with configurable failure policy,
    per-branch timeouts, and structured result aggregation.
    """

    def __init__(
        self,
        failure_policy: FailurePolicy = FailurePolicy.BEST_EFFORT,
        require_n: int = 1,
        branch_timeout_seconds: float = 30.0,
        overall_timeout_seconds: float = 60.0,
    ):
        self._policy = failure_policy
        self._require_n = require_n
        self._branch_timeout = branch_timeout_seconds
        self._overall_timeout = overall_timeout_seconds

    async def _run_branch(
        self, branch_id: str, coro: Coroutine
    ) -> BranchResult:
        t0 = time.monotonic()
        try:
            value = await asyncio.wait_for(coro, timeout=self._branch_timeout)
            return BranchResult(
                branch_id=branch_id,
                success=True,
                value=value,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
            )
        except asyncio.TimeoutError as exc:
            return BranchResult(
                branch_id=branch_id,
                success=False,
                error=exc,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
            )
        except Exception as exc:
            return BranchResult(
                branch_id=branch_id,
                success=False,
                error=exc,
                duration_ms=round((time.monotonic() - t0) * 1000, 1),
            )

    async def execute(
        self,
        branches: List[tuple],   # [(branch_id, coroutine), ...]
    ) -> FanOutResult:
        t0 = time.monotonic()

        tasks = [
            asyncio.create_task(self._run_branch(bid, coro))
            for bid, coro in branches
        ]

        try:
            results: List[BranchResult] = await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=False),
                timeout=self._overall_timeout,
            )
        except asyncio.TimeoutError:
            # Cancel remaining tasks on overall timeout
            for task in tasks:
                if not task.done():
                    task.cancel()
            done = [t.result() for t in tasks if t.done() and not t.cancelled()]
            results = done + [
                BranchResult(branch_id=f"branch_{i}", success=False,
                             error=asyncio.TimeoutError("overall timeout"))
                for i, t in enumerate(tasks) if not t.done() or t.cancelled()
            ]

        succeeded = [r for r in results if r.success]
        failed = [r for r in results if not r.success]

        policy_satisfied = self._check_policy(len(succeeded), len(branches))

        return FanOutResult(
            succeeded=succeeded,
            failed=failed,
            total_duration_ms=round((time.monotonic() - t0) * 1000, 1),
            policy_satisfied=policy_satisfied,
        )

    def _check_policy(self, n_success: int, n_total: int) -> bool:
        if self._policy == FailurePolicy.REQUIRE_ALL:
            return n_success == n_total
        if self._policy == FailurePolicy.REQUIRE_MAJORITY:
            return n_success > n_total / 2
        if self._policy == FailurePolicy.REQUIRE_N:
            return n_success >= self._require_n
        return n_success > 0   # BEST_EFFORT: at least one success
```

## Solution 2: Result Aggregator

```python
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, TypeVar

T = TypeVar("T")

class ResultAggregator:
    """
    Aggregates branch results using pluggable strategies.
    Handles heterogeneous result types (lists, dicts, scores, text).
    """

    @staticmethod
    def concat_lists(results: List[BranchResult]) -> List[Any]:
        """Merge all list results into a single flat list."""
        merged = []
        for r in results:
            if r.success and isinstance(r.value, list):
                merged.extend(r.value)
        return merged

    @staticmethod
    def merge_dicts(results: List[BranchResult], conflict: str = "last_wins") -> dict:
        """Merge all dict results. conflict='last_wins'|'first_wins'|'raise'."""
        merged: dict = {}
        for r in results:
            if not r.success or not isinstance(r.value, dict):
                continue
            for k, v in r.value.items():
                if k in merged:
                    if conflict == "raise":
                        raise ValueError(f"Conflicting key: {k}")
                    elif conflict == "first_wins":
                        continue
                merged[k] = v
        return merged

    @staticmethod
    def best_score(
        results: List[BranchResult],
        score_fn: Callable[[Any], float],
    ) -> Optional[Any]:
        """Return the result with the highest score."""
        scored = [
            (r.value, score_fn(r.value))
            for r in results if r.success and r.value is not None
        ]
        if not scored:
            return None
        return max(scored, key=lambda x: x[1])[0]

    @staticmethod
    def weighted_average(
        results: List[BranchResult],
        weight_fn: Callable[[BranchResult], float],
        value_fn: Callable[[Any], float],
    ) -> Optional[float]:
        """Weighted average of scalar results."""
        total_weight = 0.0
        total_value = 0.0
        for r in results:
            if not r.success:
                continue
            w = weight_fn(r)
            v = value_fn(r.value)
            total_weight += w
            total_value += w * v
        if total_weight == 0:
            return None
        return total_value / total_weight

    @staticmethod
    def first_success(results: List[BranchResult]) -> Optional[Any]:
        """Return the first successful result."""
        for r in results:
            if r.success:
                return r.value
        return None
```

## Solution 3: Staged Fan-Out (Dependent Branches)

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, List, Optional

@dataclass
class StageDefinition:
    stage_id: str
    branches: List[tuple]          # [(branch_id, coro_factory), ...]
    depends_on: Optional[str] = None   # stage_id this stage depends on
    policy: FailurePolicy = FailurePolicy.BEST_EFFORT

class StagedFanOutOrchestrator:
    """
    Executes fan-out stages in dependency order.
    Stage N can receive results from stage N-1 as input.
    Independent stages run in parallel; dependent stages wait.
    """

    def __init__(self, executor: FanOutExecutor):
        self._executor = executor

    async def run(
        self,
        stages: List[StageDefinition],
        initial_context: Any = None,
    ) -> Dict[str, FanOutResult]:
        results: Dict[str, FanOutResult] = {}
        context = initial_context

        # Build dependency graph
        by_id = {s.stage_id: s for s in stages}
        independent = [s for s in stages if s.depends_on is None]
        dependent = [s for s in stages if s.depends_on is not None]

        # Run independent stages in parallel
        independent_tasks = [
            asyncio.create_task(
                self._run_stage(stage, context, results)
            )
            for stage in independent
        ]
        independent_results = await asyncio.gather(*independent_tasks)
        for stage, result in zip(independent, independent_results):
            results[stage.stage_id] = result

        # Run dependent stages sequentially (respecting dependencies)
        for stage in dependent:
            dep_result = results.get(stage.depends_on)
            result = await self._run_stage(stage, dep_result, results)
            results[stage.stage_id] = result

        return results

    async def _run_stage(
        self,
        stage: StageDefinition,
        context: Any,
        prior_results: Dict[str, FanOutResult],
    ) -> FanOutResult:
        # Materialize coroutines from factories with context
        branches = []
        for bid, factory in stage.branches:
            coro = factory(context) if callable(factory) else factory
            branches.append((bid, coro))

        executor = FanOutExecutor(failure_policy=stage.policy)
        return await executor.execute(branches)
```

## Solution 4: Adaptive Fan-Out with Early Termination

```python
import asyncio
import time
from typing import Any, Callable, Coroutine, List, Optional

class AdaptiveFanOut:
    """
    Fan-out that terminates early once enough results are collected.
    Useful for hedged requests: send to N providers, accept first K responses.
    Cancels remaining branches once the threshold is met.
    """

    def __init__(
        self,
        target_results: int = 1,
        max_wait_seconds: float = 10.0,
        branch_timeout_seconds: float = 8.0,
    ):
        self._target = target_results
        self._max_wait = max_wait_seconds
        self._branch_timeout = branch_timeout_seconds

    async def execute(
        self,
        branches: List[tuple],   # [(branch_id, coroutine), ...]
    ) -> List[BranchResult]:
        results: List[BranchResult] = []
        event = asyncio.Event()

        async def run_branch(branch_id: str, coro: Coroutine) -> None:
            t0 = time.monotonic()
            try:
                value = await asyncio.wait_for(coro, timeout=self._branch_timeout)
                results.append(BranchResult(
                    branch_id=branch_id, success=True, value=value,
                    duration_ms=round((time.monotonic() - t0) * 1000, 1),
                ))
            except Exception as exc:
                results.append(BranchResult(
                    branch_id=branch_id, success=False, error=exc,
                    duration_ms=round((time.monotonic() - t0) * 1000, 1),
                ))
            if sum(1 for r in results if r.success) >= self._target:
                event.set()

        tasks = [
            asyncio.create_task(run_branch(bid, coro))
            for bid, coro in branches
        ]

        try:
            await asyncio.wait_for(event.wait(), timeout=self._max_wait)
        except asyncio.TimeoutError:
            pass

        # Cancel pending branches
        for task in tasks:
            if not task.done():
                task.cancel()

        await asyncio.gather(*tasks, return_exceptions=True)
        return [r for r in results if r.success][:self._target]
```

## Solution 5: Fan-Out Circuit Breaker

```python
import time
from dataclasses import dataclass, field
from typing import Dict

@dataclass
class BranchHealth:
    success_count: int = 0
    failure_count: int = 0
    last_failure: float = 0.0
    open_until: float = 0.0   # circuit open until this time

class FanOutCircuitBreaker:
    """
    Per-branch circuit breaker for fan-out executors.
    Skips consistently failing branches to avoid wasting latency budget.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 30.0,
    ):
        self._threshold = failure_threshold
        self._recovery = recovery_timeout_seconds
        self._health: Dict[str, BranchHealth] = {}

    def _get(self, branch_id: str) -> BranchHealth:
        if branch_id not in self._health:
            self._health[branch_id] = BranchHealth()
        return self._health[branch_id]

    def is_open(self, branch_id: str) -> bool:
        h = self._get(branch_id)
        if h.open_until > time.time():
            return True
        if h.open_until > 0:
            # Auto-reset after recovery timeout
            h.open_until = 0.0
        return False

    def record_success(self, branch_id: str) -> None:
        h = self._get(branch_id)
        h.success_count += 1
        h.failure_count = 0   # reset on success
        h.open_until = 0.0

    def record_failure(self, branch_id: str) -> None:
        h = self._get(branch_id)
        h.failure_count += 1
        h.last_failure = time.time()
        if h.failure_count >= self._threshold:
            h.open_until = time.time() + self._recovery

    def filter_branches(self, branches: list) -> tuple:
        """Returns (active_branches, skipped_branch_ids)."""
        active, skipped = [], []
        for bid, coro in branches:
            if self.is_open(bid):
                skipped.append(bid)
            else:
                active.append((bid, coro))
        return active, skipped
```

## Solution 6: Fan-Out Metrics Collector

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List

class FanOutMetricsCollector:
    def __init__(self):
        self._branch_latencies: Dict[str, List[float]] = defaultdict(list)
        self._branch_failures: Dict[str, int] = defaultdict(int)
        self._policy_violations: int = 0
        self._total_runs: int = 0

    def record(self, result: FanOutResult) -> None:
        self._total_runs += 1
        if not result.policy_satisfied:
            self._policy_violations += 1
        for r in result.succeeded:
            self._branch_latencies[r.branch_id].append(r.duration_ms)
        for r in result.failed:
            self._branch_failures[r.branch_id] += 1

    def summary(self) -> dict:
        branch_stats = {}
        for bid, latencies in self._branch_latencies.items():
            sorted_lat = sorted(latencies)
            n = len(sorted_lat)
            branch_stats[bid] = {
                "p50_ms": sorted_lat[n // 2] if n else 0,
                "p99_ms": sorted_lat[int(n * 0.99)] if n else 0,
                "success_count": n,
                "failure_count": self._branch_failures.get(bid, 0),
                "success_rate": round(n / max(n + self._branch_failures.get(bid, 0), 1), 3),
            }
        return {
            "total_runs": self._total_runs,
            "policy_violation_rate": round(self._policy_violations / max(self._total_runs, 1), 3),
            "branch_stats": branch_stats,
        }
```

## Comparison

| Approach | Partial Failure | Early Exit | Dependencies | Circuit Breaking |
|---|---|---|---|---|
| FanOutExecutor | Yes (policy) | No | No | No |
| ResultAggregator | N/A | N/A | N/A | N/A |
| StagedFanOutOrchestrator | Via executor | No | Yes | No |
| AdaptiveFanOut | N/A (best-effort) | Yes (target N) | No | No |
| FanOutCircuitBreaker | Via filtering | No | No | Yes |
| FanOutMetricsCollector | N/A (metrics) | N/A | N/A | N/A |

**Best for production**: Use `FanOutExecutor` with `BEST_EFFORT` policy for retrieval fan-outs (tolerate partial failures), `REQUIRE_MAJORITY` for consensus-sensitive operations. Wrap with `FanOutCircuitBreaker` to skip persistently failing branches automatically. Use `AdaptiveFanOut` for hedged API calls where the first response wins. Collect all execution data in `FanOutMetricsCollector` to identify slow or unreliable branches and tune timeouts per-branch.
