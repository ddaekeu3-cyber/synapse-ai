---
title: "Agent Doesn't Implement Parallel Tool Call Dependency Resolution"
description: "Agents that execute tool calls sequentially even when many are independent waste wall-clock time proportional to the number of tools: five independent lookups taking 200ms each complete in 1000ms sequentially but 200ms in parallel. Implement dependency resolution that identifies which tool calls are independent and dispatches them concurrently, while respecting declared dependencies to preserve correct ordering."
date: 2026-04-16
difficulty: advanced
category: performance
slug: agent-doesnt-implement-parallel-tool-call-dependency-resolution
tags: [parallel-execution, dependency-resolution, tool-scheduling, dag, concurrency, latency-reduction]
symptoms:
  - "Tool calls execute one at a time even when they have no data dependencies"
  - "Total tool latency is the sum of all individual latencies rather than the maximum"
  - "LLM requests sequential tool calls that could safely be parallelized"
  - "No mechanism to express that tool B must run after tool A completes"
  - "Adding more independent tools linearly increases response time"
---

## Why This Happens

Sequential execution is the default because it requires no coordination: call tool A, wait, call tool B, wait. Parallel execution requires knowing which tools are independent — either by explicit declaration or by inferring from the absence of data flow between them. Without a dependency graph, every tool call must wait for all previous calls to complete. A DAG-based scheduler identifies which tools share no data dependency and dispatches them as a group, then waits only for the specific tools whose outputs feed into the next layer.

## Solution 1: Tool Call Node and Dependency Graph

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set


@dataclass
class ToolCallSpec:
    """A single tool call with its declared dependencies."""
    call_id: str
    tool_name: str
    args: Dict[str, Any]
    depends_on: List[str] = field(default_factory=list)  # call_ids of upstream deps
    result_key: Optional[str] = None   # key to store result under for downstream use
    priority: int = 0                  # higher = schedule first among ready calls


class DependencyGraph:
    """
    Builds and validates a DAG of tool call specs.
    Computes execution layers: groups of calls that can run in parallel.
    """

    def __init__(self, specs: List[ToolCallSpec]):
        self._specs = {s.call_id: s for s in specs}
        self._validate()

    def _validate(self) -> None:
        for spec in self._specs.values():
            for dep in spec.depends_on:
                if dep not in self._specs:
                    raise ValueError(f"call '{spec.call_id}' depends on unknown call '{dep}'")
        if self._has_cycle():
            raise ValueError("dependency graph contains a cycle")

    def _has_cycle(self) -> bool:
        visited: Set[str] = set()
        rec_stack: Set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for dep in self._specs[node].depends_on:
                if dep not in visited:
                    if dfs(dep):
                        return True
                elif dep in rec_stack:
                    return True
            rec_stack.discard(node)
            return False

        return any(dfs(n) for n in self._specs if n not in visited)

    def execution_layers(self) -> List[List[ToolCallSpec]]:
        """Returns ordered layers; each layer's calls are independent of each other."""
        remaining = set(self._specs.keys())
        completed: Set[str] = set()
        layers = []

        while remaining:
            ready = [
                self._specs[cid]
                for cid in remaining
                if all(dep in completed for dep in self._specs[cid].depends_on)
            ]
            if not ready:
                raise RuntimeError("unresolvable dependency — possible cycle after validation")
            ready.sort(key=lambda s: s.priority, reverse=True)
            layers.append(ready)
            for spec in ready:
                completed.add(spec.call_id)
                remaining.discard(spec.call_id)

        return layers
```

## Solution 2: Result Store

```python
import asyncio
from typing import Any, Dict, Optional


class ToolCallResultStore:
    """
    Thread-safe store for tool call results.
    Downstream calls can retrieve upstream results by call_id or result_key.
    """

    def __init__(self):
        self._results: Dict[str, Any] = {}
        self._lock = asyncio.Lock()

    async def store(self, call_id: str, result: Any, result_key: Optional[str] = None) -> None:
        async with self._lock:
            self._results[call_id] = result
            if result_key:
                self._results[result_key] = result

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            return self._results.get(key)

    async def get_many(self, keys: list) -> dict:
        async with self._lock:
            return {k: self._results.get(k) for k in keys}

    def snapshot(self) -> dict:
        return dict(self._results)
```

## Solution 3: Layer Executor

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class LayerExecutor:
    """
    Executes a single dependency layer by dispatching all calls in parallel.
    Waits for all calls in the layer before returning, collecting results.
    """

    def __init__(
        self,
        dispatch_fn: Callable[[str, Dict[str, Any]], Any],  # (tool_name, args) -> result
        result_store: ToolCallResultStore,
        max_concurrency: int = 10,
    ):
        self._dispatch = dispatch_fn
        self._store = result_store
        self._semaphore = asyncio.Semaphore(max_concurrency)

    async def _run_one(self, spec: ToolCallSpec) -> dict:
        async with self._semaphore:
            start = time.time()
            try:
                result = await self._dispatch(spec.tool_name, spec.args)
                await self._store.store(spec.call_id, result, spec.result_key)
                return {
                    "call_id": spec.call_id,
                    "success": True,
                    "latency_ms": round((time.time() - start) * 1000, 2),
                }
            except Exception as exc:
                await self._store.store(spec.call_id, None, spec.result_key)
                return {
                    "call_id": spec.call_id,
                    "success": False,
                    "error": str(exc),
                    "latency_ms": round((time.time() - start) * 1000, 2),
                }

    async def execute_layer(self, specs: List[ToolCallSpec]) -> List[dict]:
        tasks = [self._run_one(spec) for spec in specs]
        return await asyncio.gather(*tasks)
```

## Solution 4: Parallel DAG Scheduler

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class ParallelDAGScheduler:
    """
    Orchestrates full DAG execution: computes layers, executes each layer
    in parallel, and aggregates results with per-layer and total latency stats.
    """

    def __init__(self, layer_executor: LayerExecutor, result_store: ToolCallResultStore):
        self._executor = layer_executor
        self._store = result_store

    async def run(self, specs: List[ToolCallSpec]) -> dict:
        graph = DependencyGraph(specs)
        layers = graph.execution_layers()
        total_start = time.time()
        layer_results = []
        all_call_results = []

        for layer_idx, layer in enumerate(layers):
            layer_start = time.time()
            results = await self._executor.execute_layer(layer)
            layer_latency = round((time.time() - layer_start) * 1000, 2)
            layer_results.append({
                "layer": layer_idx,
                "call_count": len(layer),
                "call_ids": [s.call_id for s in layer],
                "layer_latency_ms": layer_latency,
                "failures": [r for r in results if not r["success"]],
            })
            all_call_results.extend(results)

        total_latency = round((time.time() - total_start) * 1000, 2)
        sequential_estimate = sum(r["latency_ms"] for r in all_call_results)

        return {
            "total_latency_ms": total_latency,
            "sequential_estimate_ms": round(sequential_estimate, 2),
            "speedup_factor": round(sequential_estimate / max(total_latency, 1), 2),
            "layer_count": len(layers),
            "total_calls": len(specs),
            "layers": layer_results,
            "results": self._store.snapshot(),
            "failures": [r for r in all_call_results if not r["success"]],
        }
```

## Solution 5: Argument Dependency Injector

```python
import re
from typing import Any, Dict


class ArgumentDependencyInjector:
    """
    Allows tool call arguments to reference upstream results using
    a template syntax: {"user_id": "$results.fetch_user.id"}.
    Resolves references from the result store before dispatching.
    """

    TEMPLATE_PATTERN = re.compile(r"^\$results\.(.+)$")

    def __init__(self, result_store: ToolCallResultStore):
        self._store = result_store

    async def resolve(self, args: Dict[str, Any]) -> Dict[str, Any]:
        resolved = {}
        for key, value in args.items():
            if isinstance(value, str):
                match = self.TEMPLATE_PATTERN.match(value)
                if match:
                    path = match.group(1).split(".")
                    root_key = path[0]
                    upstream = await self._store.get(root_key)
                    for part in path[1:]:
                        if isinstance(upstream, dict):
                            upstream = upstream.get(part)
                        else:
                            upstream = None
                            break
                    resolved[key] = upstream
                    continue
            resolved[key] = value
        return resolved
```

## Solution 6: Scheduling Stats Dashboard

```python
import time
from typing import List


class ParallelSchedulingDashboard:
    """
    Aggregates DAG scheduling runs to report average parallelism,
    speedup factors, and failure rates over time.
    """

    def __init__(self):
        self._runs: List[dict] = []
        self._timestamps: List[float] = []

    def record(self, run_result: dict) -> None:
        self._runs.append(run_result)
        self._timestamps.append(time.time())

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [
            r for r, ts in zip(self._runs, self._timestamps)
            if ts >= cutoff
        ]
        if not recent:
            return {"window_seconds": window_seconds, "runs": 0}

        avg_speedup = sum(r["speedup_factor"] for r in recent) / len(recent)
        avg_layers = sum(r["layer_count"] for r in recent) / len(recent)
        total_failures = sum(len(r["failures"]) for r in recent)
        total_calls = sum(r["total_calls"] for r in recent)

        return {
            "window_seconds": window_seconds,
            "runs": len(recent),
            "avg_speedup_factor": round(avg_speedup, 2),
            "avg_layer_count": round(avg_layers, 2),
            "total_tool_calls": total_calls,
            "total_failures": total_failures,
            "failure_rate": round(total_failures / max(total_calls, 1), 4),
            "avg_total_latency_ms": round(
                sum(r["total_latency_ms"] for r in recent) / len(recent), 2
            ),
        }
```

## Comparison

| Approach | DAG Validation | Layer Computation | Parallel Dispatch | Result Injection | Stats |
|---|---|---|---|---|---|
| DependencyGraph | Yes (cycle detect) | Yes (topological) | No | No | No |
| LayerExecutor | No | No | Yes (semaphore) | No | No |
| ParallelDAGScheduler | Via graph | Via graph | Via executor | No | No |
| ArgumentDependencyInjector | No | No | No | Yes ($results) | No |
| ParallelSchedulingDashboard | No | No | No | No | Yes |

**Best for production**: Declare dependencies explicitly in `ToolCallSpec.depends_on` — do not try to infer them from argument names. Set `max_concurrency=10` in `LayerExecutor` to prevent thundering-herd against downstream services when a large layer triggers. Use `ArgumentDependencyInjector` to thread upstream results into downstream args without manual plumbing — this also makes the dependency graph self-documenting. Monitor `avg_speedup_factor` in the dashboard: values near 1.0 indicate that the LLM is generating sequential dependency chains where none are needed, which is a prompt engineering problem rather than a scheduling problem.
