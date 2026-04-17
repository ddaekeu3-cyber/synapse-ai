---
title: "Agent Doesn't Implement Dependency Health Precheck Before Task Start"
description: "Agents that begin executing multi-step tasks without checking whether required dependencies are available will fail mid-task after completing irreversible steps: a ten-step workflow that writes to a database on step eight fails there if the database was down at the start, wasting seven steps of computation and leaving state partially applied. Implement a dependency health precheck that validates all required services are reachable before any task step executes, failing fast with a clear diagnostic rather than mid-task."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-dependency-health-precheck-before-task-start
tags: [health-precheck, dependency-validation, fail-fast, pre-flight-check, task-reliability, service-availability]
symptoms:
  - "Multi-step tasks fail mid-execution when a dependency becomes unavailable after step 3 of 10"
  - "Irreversible actions (emails sent, payments charged) complete before the failing dependency is reached"
  - "No pre-flight check validates that required services are up before committing to a task"
  - "Users receive confusing mid-task error messages instead of an upfront 'service unavailable' response"
  - "Partial task state requires manual cleanup after a dependency failure midway through execution"
---

## Why This Happens

Agents are optimistic by default: they assume the environment is healthy and begin executing immediately. When a required dependency (database, external API, queue, secret store) is unavailable, the failure occurs at the point where that dependency is first used — which may be deep into a multi-step task. The fail-fast principle argues for checking all preconditions before committing to any action. A dependency precheck runs lightweight probes against each required service, collects results, and either approves the task to proceed or returns a structured failure that tells the user exactly which dependency prevented the task from starting.

## Solution 1: Dependency Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional


class DependencyKind(str, Enum):
    DATABASE = "database"
    HTTP_SERVICE = "http_service"
    MESSAGE_QUEUE = "message_queue"
    SECRET_STORE = "secret_store"
    FILE_SYSTEM = "file_system"
    LLM_PROVIDER = "llm_provider"
    CACHE = "cache"
    CUSTOM = "custom"


class DependencyRequirement(str, Enum):
    REQUIRED = "required"        # task cannot start without this dependency
    PREFERRED = "preferred"      # task can degrade gracefully if unavailable
    OPTIONAL = "optional"        # task continues normally if unavailable


@dataclass
class DependencyDescriptor:
    name: str
    kind: DependencyKind
    requirement: DependencyRequirement
    probe_fn: Callable            # async fn() -> bool (True = healthy)
    timeout_seconds: float = 3.0
    description: str = ""
    tags: List[str] = field(default_factory=list)
```

## Solution 2: Dependency Probe Result

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ProbeStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    TIMEOUT = "timeout"
    ERROR = "error"


@dataclass
class DependencyProbeResult:
    name: str
    kind: DependencyKind
    requirement: DependencyRequirement
    status: ProbeStatus
    latency_ms: float
    error: Optional[str] = None
    probed_at: float = field(default_factory=time.time)

    @property
    def is_healthy(self) -> bool:
        return self.status == ProbeStatus.HEALTHY

    @property
    def blocks_task(self) -> bool:
        return (
            self.requirement == DependencyRequirement.REQUIRED
            and not self.is_healthy
        )
```

## Solution 3: Dependency Prober

```python
import asyncio
import time
from typing import List


class DependencyProber:
    """
    Runs probes against all registered dependencies concurrently.
    Returns results for each dependency within the configured timeout.
    """

    async def probe(self, dependency: DependencyDescriptor) -> DependencyProbeResult:
        start = time.monotonic()
        try:
            healthy = await asyncio.wait_for(
                dependency.probe_fn(),
                timeout=dependency.timeout_seconds,
            )
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            status = ProbeStatus.HEALTHY if healthy else ProbeStatus.UNAVAILABLE
            return DependencyProbeResult(
                name=dependency.name,
                kind=dependency.kind,
                requirement=dependency.requirement,
                status=status,
                latency_ms=latency_ms,
            )
        except asyncio.TimeoutError:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            return DependencyProbeResult(
                name=dependency.name,
                kind=dependency.kind,
                requirement=dependency.requirement,
                status=ProbeStatus.TIMEOUT,
                latency_ms=latency_ms,
                error=f"probe timed out after {dependency.timeout_seconds}s",
            )
        except Exception as exc:
            latency_ms = round((time.monotonic() - start) * 1000, 2)
            return DependencyProbeResult(
                name=dependency.name,
                kind=dependency.kind,
                requirement=dependency.requirement,
                status=ProbeStatus.ERROR,
                latency_ms=latency_ms,
                error=str(exc),
            )

    async def probe_all(
        self, dependencies: List[DependencyDescriptor]
    ) -> List[DependencyProbeResult]:
        tasks = [self.probe(dep) for dep in dependencies]
        return await asyncio.gather(*tasks)
```

## Solution 4: Precheck Decision Engine

```python
from dataclasses import dataclass
from typing import List, Optional


@dataclass
class PrecheckDecision:
    approved: bool
    blocking_failures: List[DependencyProbeResult]
    degraded_dependencies: List[DependencyProbeResult]
    healthy_dependencies: List[DependencyProbeResult]
    rejection_summary: str = ""

    @property
    def can_proceed(self) -> bool:
        return self.approved

    def user_message(self) -> str:
        if self.approved and not self.degraded_dependencies:
            return "All dependencies healthy. Task approved to proceed."
        if self.approved:
            degraded_names = [d.name for d in self.degraded_dependencies]
            return (
                f"Task approved with degraded dependencies: {', '.join(degraded_names)}. "
                "Some features may be limited."
            )
        blocked_names = [d.name for d in self.blocking_failures]
        return (
            f"Task cannot start: required dependencies unavailable: "
            f"{', '.join(blocked_names)}. {self.rejection_summary}"
        )


class PrecheckDecisionEngine:
    """
    Evaluates probe results and produces a go/no-go decision for task execution.
    """

    def evaluate(self, results: List[DependencyProbeResult]) -> PrecheckDecision:
        blocking = [r for r in results if r.blocks_task]
        degraded = [
            r for r in results
            if not r.is_healthy and not r.blocks_task
        ]
        healthy = [r for r in results if r.is_healthy]

        rejection_summary = ""
        if blocking:
            errors = [
                f"{r.name} ({r.status.value}: {r.error or 'no details'})"
                for r in blocking
            ]
            rejection_summary = "Failures: " + "; ".join(errors)

        return PrecheckDecision(
            approved=len(blocking) == 0,
            blocking_failures=blocking,
            degraded_dependencies=degraded,
            healthy_dependencies=healthy,
            rejection_summary=rejection_summary,
        )
```

## Solution 5: Task Precheck Orchestrator

```python
import time
from typing import List, Optional


class TaskPrecheckOrchestrator:
    """
    Runs dependency probes before task execution starts.
    Records precheck results for post-incident analysis.
    """

    def __init__(
        self,
        prober: DependencyProber,
        engine: PrecheckDecisionEngine,
        audit_logger: "PrecheckAuditLogger",
    ):
        self._prober = prober
        self._engine = engine
        self._logger = audit_logger

    async def run(
        self,
        task_id: str,
        dependencies: List[DependencyDescriptor],
        session_id: str = "",
    ) -> PrecheckDecision:
        probe_start = time.monotonic()
        results = await self._prober.probe_all(dependencies)
        probe_duration_ms = round((time.monotonic() - probe_start) * 1000, 2)

        decision = self._engine.evaluate(results)
        self._logger.record(task_id, session_id, decision, probe_duration_ms)
        return decision

    async def guard(
        self,
        task_id: str,
        dependencies: List[DependencyDescriptor],
        session_id: str = "",
    ) -> None:
        """Raises TaskBlockedError if the precheck fails."""
        decision = await self.run(task_id, dependencies, session_id)
        if not decision.approved:
            raise TaskBlockedByDependencyError(task_id, decision)


class TaskBlockedByDependencyError(Exception):
    def __init__(self, task_id: str, decision: PrecheckDecision):
        super().__init__(decision.user_message())
        self.task_id = task_id
        self.decision = decision
```

## Solution 6: Precheck Audit Logger

```python
import time
from typing import List


class PrecheckAuditLogger:
    """
    Records precheck decisions for reliability analysis and incident investigation.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        task_id: str,
        session_id: str,
        decision: PrecheckDecision,
        probe_duration_ms: float,
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "task_id": task_id,
            "session_id": session_id,
            "approved": decision.approved,
            "probe_duration_ms": probe_duration_ms,
            "blocking_count": len(decision.blocking_failures),
            "degraded_count": len(decision.degraded_dependencies),
            "blocking_names": [r.name for r in decision.blocking_failures],
            "degraded_names": [r.name for r in decision.degraded_dependencies],
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "prechecks": 0}
        blocked = [r for r in recent if not r["approved"]]
        dep_failure_counts: dict = {}
        for r in blocked:
            for name in r["blocking_names"]:
                dep_failure_counts[name] = dep_failure_counts.get(name, 0) + 1
        return {
            "window_seconds": window_seconds,
            "prechecks": len(recent),
            "blocked": len(blocked),
            "block_rate": round(len(blocked) / len(recent), 4),
            "avg_probe_ms": round(
                sum(r["probe_duration_ms"] for r in recent) / len(recent), 2
            ),
            "most_blocking_deps": sorted(
                dep_failure_counts.items(), key=lambda kv: kv[1], reverse=True
            )[:5],
        }
```

## Comparison

| Approach | Concurrent Probing | Go/No-Go Decision | Degraded Handling | Fast-Fail Exception | Audit Log |
|---|---|---|---|---|---|
| DependencyProber | Yes (asyncio.gather) | No | No | No | No |
| PrecheckDecisionEngine | No | Yes | Yes (preferred) | No | No |
| TaskPrecheckOrchestrator | Via prober | Via engine | Via engine | Yes (guard()) | Via logger |
| PrecheckAuditLogger | No | No | No | No | Yes |

**Best for production**: Set `timeout_seconds=3.0` for all probes — a precheck that takes longer than three seconds defeats the purpose of failing fast. Run probes concurrently via `asyncio.gather` so a ten-dependency precheck completes in 3 seconds rather than 30. Use `DependencyRequirement.PREFERRED` for enrichment services (analytics APIs, secondary knowledge bases) that the agent can work without at reduced quality — blocking the task on a non-essential service is worse than running without it. Monitor `block_rate` via the audit logger: a sustained rate above 0.02 (2%) indicates a dependency is chronically unhealthy and should be escalated to the owning team rather than accepted as normal.
