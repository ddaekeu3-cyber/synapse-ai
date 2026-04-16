---
title: "Agent Doesn't Implement Dependency Health Aggregation Across Tool Integrations"
description: "Agents that call tools without a consolidated view of which dependencies are healthy, degraded, or down make it impossible to reason about overall system state: a single downstream outage may silently degrade multiple tools that share the same dependency, and operators have no way to know which tools are affected without checking each one individually. Implement dependency health aggregation that maps each tool to its dependencies, polls health in aggregate, and surfaces a single system-wide health view."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-dependency-health-aggregation-across-tool-integrations
tags: [dependency-health, health-aggregation, tool-dependencies, system-health, degraded-mode, dependency-graph]
symptoms:
  - "A single downstream outage affects multiple tools with no consolidated alert"
  - "No way to determine which tools share a common dependency that is currently failing"
  - "Overall agent health is reported as 'ok' while several tools are silently degraded"
  - "Operators check each tool individually to diagnose a systemic outage"
  - "No dependency graph exists — tool-to-service relationships are implicit in code"
---

## Why This Happens

Tools are typically implemented as isolated units. Each tool manages its own HTTP client, database connection, or SDK instance. When a shared dependency fails — the same Redis cluster used by three tools, or the same third-party API used by five — each tool fails independently with no cross-tool visibility. An operator observing a flood of tool errors has to read each error to reconstruct which dependency is common. Dependency health aggregation inverts this: each tool declares its dependencies, a central health aggregator polls or receives health updates from each dependency, and the system can answer "which tools are affected by this outage" in one query.

## Solution 1: Dependency Declaration

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, List, Optional


class DependencyKind(str, Enum):
    HTTP_API = "http_api"
    DATABASE = "database"
    CACHE = "cache"
    MESSAGE_QUEUE = "message_queue"
    FILE_SYSTEM = "file_system"
    EXTERNAL_SDK = "external_sdk"


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    DOWN = "down"
    UNKNOWN = "unknown"


@dataclass
class DependencyDeclaration:
    dependency_id: str
    display_name: str
    kind: DependencyKind
    health_check_fn: Callable[[], bool]
    critical: bool = True              # if True, tool is non-functional when down
    tools_using: List[str] = field(default_factory=list)
    timeout_seconds: float = 5.0
```

## Solution 2: Dependency Health Poller

```python
import asyncio
import time
from typing import Dict, Optional


class DependencyHealthPoller:
    """
    Polls registered dependencies at a configurable interval and
    maintains the last known health status for each.
    """

    def __init__(self, poll_interval_seconds: float = 30.0):
        self._interval = poll_interval_seconds
        self._declarations: Dict[str, DependencyDeclaration] = {}
        self._statuses: Dict[str, HealthStatus] = {}
        self._last_checked: Dict[str, float] = {}
        self._consecutive_failures: Dict[str, int] = {}
        self._running = False

    def register(self, declaration: DependencyDeclaration) -> None:
        self._declarations[declaration.dependency_id] = declaration
        self._statuses[declaration.dependency_id] = HealthStatus.UNKNOWN
        self._consecutive_failures[declaration.dependency_id] = 0

    async def _check_one(self, dep: DependencyDeclaration) -> HealthStatus:
        try:
            ok = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(None, dep.health_check_fn),
                timeout=dep.timeout_seconds,
            )
            return HealthStatus.HEALTHY if ok else HealthStatus.DEGRADED
        except Exception:
            return HealthStatus.DOWN

    async def poll_once(self) -> Dict[str, HealthStatus]:
        results = {}
        for dep_id, dep in self._declarations.items():
            status = await self._check_one(dep)
            if status == HealthStatus.DOWN:
                self._consecutive_failures[dep_id] = self._consecutive_failures.get(dep_id, 0) + 1
            else:
                self._consecutive_failures[dep_id] = 0
            self._statuses[dep_id] = status
            self._last_checked[dep_id] = time.time()
            results[dep_id] = status
        return results

    async def start_polling(self) -> None:
        self._running = True
        while self._running:
            await self.poll_once()
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False

    def get_status(self, dependency_id: str) -> HealthStatus:
        return self._statuses.get(dependency_id, HealthStatus.UNKNOWN)

    def all_statuses(self) -> Dict[str, HealthStatus]:
        return dict(self._statuses)
```

## Solution 3: Tool Dependency Mapper

```python
from typing import Dict, List, Optional, Set


class ToolDependencyMapper:
    """
    Maps tools to their declared dependencies and supports
    reverse lookup: given a dependency, which tools are affected?
    """

    def __init__(self):
        self._tool_deps: Dict[str, List[str]] = {}      # tool -> [dep_ids]
        self._dep_tools: Dict[str, Set[str]] = {}       # dep_id -> {tools}

    def register_tool(self, tool_name: str, dependency_ids: List[str]) -> None:
        self._tool_deps[tool_name] = dependency_ids
        for dep_id in dependency_ids:
            self._dep_tools.setdefault(dep_id, set()).add(tool_name)

    def tools_for_dependency(self, dependency_id: str) -> List[str]:
        return sorted(self._dep_tools.get(dependency_id, set()))

    def dependencies_for_tool(self, tool_name: str) -> List[str]:
        return self._tool_deps.get(tool_name, [])

    def affected_tools(self, failed_dependency_ids: List[str]) -> Dict[str, List[str]]:
        """Returns {tool_name: [failed_dep_ids]} for all tools touching any failed dep."""
        result: Dict[str, List[str]] = {}
        for dep_id in failed_dependency_ids:
            for tool in self.tools_for_dependency(dep_id):
                result.setdefault(tool, []).append(dep_id)
        return result
```

## Solution 4: Dependency Health Aggregator

```python
import time
from typing import Dict, List


class DependencyHealthAggregator:
    """
    Combines poller output and dependency mapper to produce a
    system-wide health view: which dependencies are down,
    which tools are affected, and overall health grade.
    """

    def __init__(
        self,
        poller: DependencyHealthPoller,
        mapper: ToolDependencyMapper,
    ):
        self._poller = poller
        self._mapper = mapper

    def system_health(self) -> dict:
        statuses = self._poller.all_statuses()
        down = [dep for dep, s in statuses.items() if s == HealthStatus.DOWN]
        degraded = [dep for dep, s in statuses.items() if s == HealthStatus.DEGRADED]
        healthy = [dep for dep, s in statuses.items() if s == HealthStatus.HEALTHY]

        affected = self._mapper.affected_tools(down + degraded)
        critical_outages = [
            dep for dep in down
            if self._poller._declarations.get(dep, DependencyDeclaration(
                dep, dep, DependencyKind.HTTP_API, lambda: True
            )).critical
        ]

        if critical_outages:
            grade = "critical"
        elif down:
            grade = "degraded"
        elif degraded:
            grade = "warning"
        else:
            grade = "healthy"

        return {
            "generated_at": time.time(),
            "grade": grade,
            "dependency_counts": {
                "healthy": len(healthy),
                "degraded": len(degraded),
                "down": len(down),
                "unknown": len([s for s in statuses.values() if s == HealthStatus.UNKNOWN]),
            },
            "down_dependencies": down,
            "degraded_dependencies": degraded,
            "affected_tools": affected,
            "critical_outages": critical_outages,
        }
```

## Solution 5: Tool Health Gate

```python
from typing import Any, Callable, List, Optional


class ToolHealthGate:
    """
    Checks dependency health before dispatching a tool call.
    Blocks calls when critical dependencies are down and the
    tool has no degraded-mode fallback.
    """

    def __init__(
        self,
        poller: DependencyHealthPoller,
        mapper: ToolDependencyMapper,
    ):
        self._poller = poller
        self._mapper = mapper

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        degraded_fallback: Optional[Callable] = None,
        **kwargs: Any,
    ) -> Any:
        dep_ids = self._mapper.dependencies_for_tool(tool_name)
        critical_down = [
            dep_id for dep_id in dep_ids
            if self._poller.get_status(dep_id) == HealthStatus.DOWN
            and self._poller._declarations.get(dep_id) is not None
            and self._poller._declarations[dep_id].critical
        ]

        if critical_down:
            if degraded_fallback is not None:
                return await degraded_fallback(*args, **kwargs)
            raise DependencyDownError(tool_name, critical_down)

        return await tool_fn(*args, **kwargs)


class DependencyDownError(Exception):
    def __init__(self, tool_name: str, down_deps: List[str]):
        super().__init__(
            f"tool '{tool_name}' blocked: critical dependencies down: {down_deps}"
        )
        self.tool_name = tool_name
        self.down_dependencies = down_deps
```

## Solution 6: Dependency Health Dashboard

```python
import time


class DependencyHealthDashboard:
    """
    Full operational snapshot: per-dependency status, affected tools,
    consecutive failure counts, and system health grade.
    """

    def __init__(
        self,
        aggregator: DependencyHealthAggregator,
        poller: DependencyHealthPoller,
    ):
        self._aggregator = aggregator
        self._poller = poller

    def render(self) -> dict:
        system = self._aggregator.system_health()
        per_dep = {}
        for dep_id, decl in self._poller._declarations.items():
            per_dep[dep_id] = {
                "display_name": decl.display_name,
                "kind": decl.kind.value,
                "status": self._poller.get_status(dep_id).value,
                "critical": decl.critical,
                "consecutive_failures": self._poller._consecutive_failures.get(dep_id, 0),
                "last_checked": self._poller._last_checked.get(dep_id),
                "tools_using": decl.tools_using,
            }
        system["per_dependency"] = per_dep
        return system
```

## Comparison

| Approach | Dependency Polling | Reverse Lookup | Tool Blocking | Degraded Fallback | Dashboard |
|---|---|---|---|---|---|
| DependencyHealthPoller | Yes (async) | No | No | No | No |
| ToolDependencyMapper | No | Yes | No | No | No |
| DependencyHealthAggregator | Via poller | Via mapper | No | No | Yes (grade) |
| ToolHealthGate | Via poller | Via mapper | Yes | Yes | No |
| DependencyHealthDashboard | No | No | No | No | Yes (full) |

**Best for production**: Register all tool dependencies at startup using `DependencyDeclaration` so the dependency graph is explicit and auditable. Set `critical=False` for dependencies that have cached or degraded fallbacks — this prevents unnecessary blocking. Run `DependencyHealthPoller` at a 30-second interval and surface `system_health()["grade"]` as a single metric on the on-call dashboard: a transition from `healthy` to `degraded` in this grade is the leading indicator for tool failure alerts that will follow within seconds.
