---
title: "Agent Doesn't Implement Graceful Degradation When Tools Are Unavailable"
description: "Agents that treat every tool failure as fatal — raising an exception and halting the workflow — fail completely when a non-critical tool is temporarily unavailable. A web search outage should not prevent the agent from answering from its knowledge base; a database lookup failure should not block a response that doesn't require that data. Implement graceful degradation strategies that classify tools by criticality, provide fallback paths, and allow partial completion when secondary tools fail."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-graceful-degradation-when-tools-are-unavailable
tags: [graceful-degradation, tool-fallback, partial-completion, fault-tolerance, tool-criticality, resilience]
symptoms:
  - "Agent returns a hard error to the user when a non-critical enrichment tool fails"
  - "A single tool timeout causes the entire multi-step workflow to abort"
  - "No distinction between tools that are required for correctness and tools that only enhance the response"
  - "Tool failures propagate as unhandled exceptions rather than being routed to fallback logic"
  - "Users receive 500 errors for temporary outages that could have been handled with a degraded response"
---

## Why This Happens

Agents are typically built with a happy-path architecture: every tool call is expected to succeed, and exceptions bubble up to a top-level error handler that returns a generic failure. When the system grows to dozens of tools, the assumption that all tools are equally critical breaks down. Some tools — like a knowledge base lookup — are required; others — like a sentiment enrichment or a related-article suggester — are optional. Graceful degradation requires classifying each tool, defining fallback behaviors, and building a dispatcher that routes failures to those fallbacks rather than propagating them upward.

## Solution 1: Tool Criticality Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, List, Optional


class ToolCriticality(str, Enum):
    REQUIRED = "required"       # failure aborts the workflow
    PREFERRED = "preferred"     # failure triggers a fallback; no fallback = partial result
    OPTIONAL = "optional"       # failure is silently tolerated; result omitted


@dataclass
class ToolDegradationPolicy:
    tool_name: str
    criticality: ToolCriticality
    fallback_fn: Optional[Callable] = None     # async fn with same signature
    fallback_value: Any = None                 # static value if no fallback_fn
    max_retries: int = 1
    timeout_seconds: float = 10.0
    degradation_message: str = ""              # message to include in partial response


@dataclass
class DegradedResult:
    tool_name: str
    success: bool
    value: Any
    degraded: bool = False
    fallback_used: bool = False
    error: str = ""
    omitted: bool = False
```

## Solution 2: Degradation Policy Registry

```python
from threading import Lock
from typing import Dict, Optional


class DegradationPolicyRegistry:
    """
    Stores per-tool degradation policies. Tools not explicitly registered
    default to PREFERRED criticality with no fallback.
    """

    def __init__(self):
        self._policies: Dict[str, ToolDegradationPolicy] = {}
        self._lock = Lock()

    def register(self, policy: ToolDegradationPolicy) -> None:
        with self._lock:
            self._policies[policy.tool_name] = policy

    def get(self, tool_name: str) -> ToolDegradationPolicy:
        with self._lock:
            return self._policies.get(
                tool_name,
                ToolDegradationPolicy(
                    tool_name=tool_name,
                    criticality=ToolCriticality.PREFERRED,
                ),
            )

    def all_policies(self) -> Dict[str, ToolDegradationPolicy]:
        with self._lock:
            return dict(self._policies)
```

## Solution 3: Graceful Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable


class GracefulToolDispatcher:
    """
    Executes a tool according to its degradation policy.
    REQUIRED tools propagate failures; PREFERRED tools try fallbacks;
    OPTIONAL tools swallow failures and return omitted=True.
    """

    def __init__(self, registry: DegradationPolicyRegistry):
        self._registry = registry

    async def dispatch(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> DegradedResult:
        policy = self._registry.get(tool_name)

        for attempt in range(policy.max_retries + 1):
            try:
                value = await asyncio.wait_for(
                    tool_fn(*args, **kwargs),
                    timeout=policy.timeout_seconds,
                )
                return DegradedResult(
                    tool_name=tool_name,
                    success=True,
                    value=value,
                )
            except Exception as exc:
                if attempt < policy.max_retries:
                    await asyncio.sleep(0.5 * (attempt + 1))
                    continue
                return await self._handle_failure(policy, exc, args, kwargs)

    async def _handle_failure(
        self,
        policy: ToolDegradationPolicy,
        exc: Exception,
        args: tuple,
        kwargs: dict,
    ) -> DegradedResult:
        error_msg = str(exc)

        if policy.criticality == ToolCriticality.REQUIRED:
            raise ToolRequiredError(policy.tool_name, error_msg) from exc

        if policy.criticality == ToolCriticality.OPTIONAL:
            return DegradedResult(
                tool_name=policy.tool_name,
                success=False,
                value=None,
                degraded=True,
                omitted=True,
                error=error_msg,
            )

        # PREFERRED: try fallback
        if policy.fallback_fn is not None:
            try:
                fallback_value = await policy.fallback_fn(*args, **kwargs)
                return DegradedResult(
                    tool_name=policy.tool_name,
                    success=True,
                    value=fallback_value,
                    degraded=True,
                    fallback_used=True,
                )
            except Exception:
                pass

        if policy.fallback_value is not None:
            return DegradedResult(
                tool_name=policy.tool_name,
                success=True,
                value=policy.fallback_value,
                degraded=True,
                fallback_used=True,
            )

        return DegradedResult(
            tool_name=policy.tool_name,
            success=False,
            value=None,
            degraded=True,
            omitted=True,
            error=error_msg,
        )


class ToolRequiredError(Exception):
    def __init__(self, tool_name: str, reason: str):
        super().__init__(f"required tool '{tool_name}' failed: {reason}")
        self.tool_name = tool_name
```

## Solution 4: Partial Completion Aggregator

```python
from typing import Dict, List, Optional


class PartialCompletionAggregator:
    """
    Collects DegradedResult objects from multiple tool calls and produces
    a structured summary indicating which tools succeeded, which degraded,
    and which were omitted — for inclusion in the agent's response.
    """

    def __init__(self):
        self._results: List[DegradedResult] = []

    def add(self, result: DegradedResult) -> None:
        self._results.append(result)

    def all_required_succeeded(self, registry: DegradationPolicyRegistry) -> bool:
        for result in self._results:
            policy = registry.get(result.tool_name)
            if policy.criticality == ToolCriticality.REQUIRED and not result.success:
                return False
        return True

    def degradation_summary(self) -> dict:
        succeeded = [r for r in self._results if r.success and not r.degraded]
        degraded = [r for r in self._results if r.degraded and not r.omitted]
        omitted = [r for r in self._results if r.omitted]
        return {
            "total_tools": len(self._results),
            "succeeded": len(succeeded),
            "degraded_with_fallback": len(degraded),
            "omitted": len(omitted),
            "omitted_tools": [r.tool_name for r in omitted],
            "is_partial": len(omitted) > 0 or len(degraded) > 0,
        }

    def values(self) -> Dict[str, Any]:
        return {
            r.tool_name: r.value
            for r in self._results
            if r.value is not None
        }
```

## Solution 5: Degradation Event Logger

```python
import time
from collections import defaultdict
from typing import Dict, List


class DegradationEventLogger:
    """
    Records degradation events for post-incident analysis.
    Surfaces which tools degrade most frequently.
    """

    def __init__(self, max_records: int = 5000):
        self._max = max_records
        self._records: List[dict] = []

    def record(self, result: DegradedResult) -> None:
        if not result.degraded:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": result.tool_name,
            "omitted": result.omitted,
            "fallback_used": result.fallback_used,
            "error": result.error,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        by_tool: Dict[str, int] = defaultdict(int)
        for r in recent:
            by_tool[r["tool_name"]] += 1
        return {
            "window_seconds": window_seconds,
            "degradation_events": len(recent),
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: x[1], reverse=True)),
        }
```

## Solution 6: Graceful Degradation Dashboard

```python
import time


class GracefulDegradationDashboard:
    """
    Combines policy registry, live degradation events, and partial
    completion stats into a single operational snapshot.
    """

    def __init__(
        self,
        registry: DegradationPolicyRegistry,
        logger: DegradationEventLogger,
    ):
        self._registry = registry
        self._logger = logger

    def render(self, window_seconds: float = 3600.0) -> dict:
        policies = self._registry.all_policies()
        return {
            "generated_at": time.time(),
            "registered_tools": {
                name: p.criticality.value
                for name, p in policies.items()
            },
            "degradation_events": self._logger.summary(window_seconds),
        }
```

## Comparison

| Approach | Criticality Classification | Fallback Execution | Partial Completion | Event Logging | Dashboard |
|---|---|---|---|---|---|
| ToolDegradationPolicy | Yes | Via fallback_fn | No | No | No |
| DegradationPolicyRegistry | Via policies | No | No | No | No |
| GracefulToolDispatcher | Via registry | Yes (retry + fallback) | No | No | No |
| PartialCompletionAggregator | No | No | Yes | No | No |
| DegradationEventLogger | No | No | No | Yes | No |
| GracefulDegradationDashboard | No | No | No | No | Yes |

**Best for production**: Classify tools into exactly three tiers at registration time — avoid a fourth "nice to have" tier that creates ambiguity. For PREFERRED tools, always provide either a `fallback_fn` or a `fallback_value` (e.g., an empty list or a cached result from the last successful call). Include the `degradation_summary()` in every agent response as a structured metadata field — clients and dashboards can surface "This response is based on partial data: web_search was unavailable" without polluting the answer text. Alert when `DegradationEventLogger.summary()["degradation_events"]` exceeds a threshold — a spike usually indicates a dependency outage that merits investigation even if the agent is still serving degraded responses.
