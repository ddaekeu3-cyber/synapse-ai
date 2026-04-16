---
title: "Agent Doesn't Implement Graceful Degradation for Non-Critical Tool Failures"
description: "Agents that treat every tool failure as fatal abort the entire response when an optional enrichment tool fails: a weather lookup error causes the whole travel planning response to fail, a news tool timeout aborts a research summary. Implement graceful degradation that classifies tools as critical or non-critical, continues execution when non-critical tools fail, and informs the LLM of which context is unavailable so it can produce a useful partial response."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-graceful-degradation-for-non-critical-tool-failures
tags: [graceful-degradation, fault-tolerance, partial-response, non-critical-tools, optional-enrichment, resilience]
symptoms:
  - "Entire agent response fails when an optional enrichment tool times out"
  - "No distinction between required tools and best-effort context tools"
  - "LLM receives no indication that some context is missing — hallucinates instead"
  - "Tool failure cascade: one slow tool blocks all others from contributing"
  - "Users receive error messages for requests that could be partially answered"
---

## Why This Happens

Tool orchestrators typically `await` each tool call and propagate exceptions upward. When one tool throws, the exception unwinds the call stack and the entire response is abandoned. The fix requires classifying tools by criticality before dispatch: critical tools (database lookups, identity verification) must succeed; non-critical tools (weather, news, recommendations) should contribute if available but must not block the response. Graceful degradation means collecting partial results, recording which tools failed and why, and injecting that information into the LLM context so it can explicitly acknowledge the gap.

## Solution 1: Tool Criticality Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional


class ToolCriticality(str, Enum):
    CRITICAL = "critical"         # failure aborts the response
    IMPORTANT = "important"       # failure degrades quality; warn in context
    OPTIONAL = "optional"         # failure is silent; LLM proceeds without result
    ENRICHMENT = "enrichment"     # best-effort; timeout is expected; never blocks


@dataclass
class ToolCriticalityDescriptor:
    tool_name: str
    criticality: ToolCriticality
    timeout_seconds: float = 10.0
    fallback_value: Any = None    # returned on failure for non-critical tools
    failure_message: str = ""     # injected into context on failure
```

## Solution 2: Degraded Tool Result

```python
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class DegradedToolResult:
    tool_name: str
    success: bool
    value: Any = None
    error: Optional[str] = None
    fallback_used: bool = False
    criticality: str = "optional"
    duration_ms: float = 0.0

    def context_note(self) -> str:
        """Returns a note to inject into LLM context when the tool failed."""
        if self.success:
            return ""
        if self.criticality == "optional":
            return f"[{self.tool_name}: unavailable]"
        return f"[{self.tool_name}: failed — {self.error or 'unknown error'}. Proceed without this data.]"
```

## Solution 3: Graceful Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class GracefulToolDispatcher:
    """
    Dispatches tool calls according to their criticality descriptor.
    Critical tool failures raise immediately. Non-critical failures
    return DegradedToolResult with fallback_value.
    """

    def __init__(self, descriptors: Dict[str, ToolCriticalityDescriptor]):
        self._descriptors = descriptors

    def _get_descriptor(self, tool_name: str) -> ToolCriticalityDescriptor:
        return self._descriptors.get(
            tool_name,
            ToolCriticalityDescriptor(
                tool_name=tool_name,
                criticality=ToolCriticality.OPTIONAL,
            ),
        )

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> DegradedToolResult:
        descriptor = self._get_descriptor(tool_name)
        start = time.time()

        try:
            result = await asyncio.wait_for(
                tool_fn(*args, **kwargs),
                timeout=descriptor.timeout_seconds,
            )
            return DegradedToolResult(
                tool_name=tool_name,
                success=True,
                value=result,
                criticality=descriptor.criticality.value,
                duration_ms=round((time.time() - start) * 1000, 2),
            )
        except Exception as exc:
            duration_ms = round((time.time() - start) * 1000, 2)
            if descriptor.criticality == ToolCriticality.CRITICAL:
                raise
            return DegradedToolResult(
                tool_name=tool_name,
                success=False,
                value=descriptor.fallback_value,
                error=str(exc),
                fallback_used=descriptor.fallback_value is not None,
                criticality=descriptor.criticality.value,
                duration_ms=duration_ms,
            )

    async def call_many(
        self,
        calls: List[tuple],  # [(tool_name, tool_fn, args, kwargs), ...]
    ) -> List[DegradedToolResult]:
        tasks = [
            self.call(name, fn, *args, **kwargs)
            for name, fn, args, kwargs in calls
        ]
        return list(await asyncio.gather(*tasks, return_exceptions=False))
```

## Solution 4: Partial Context Assembler

```python
from typing import List, Tuple


class PartialContextAssembler:
    """
    Assembles LLM context from a mix of successful and failed tool results.
    Injects degradation notes for failed non-critical tools so the LLM
    can explicitly acknowledge missing context rather than hallucinating.
    """

    def assemble(
        self,
        results: List[DegradedToolResult],
    ) -> Tuple[str, dict]:
        """
        Returns (context_string, degradation_report).
        """
        parts = []
        degradation_report = {
            "total_tools": len(results),
            "succeeded": 0,
            "failed": 0,
            "fallback_used": 0,
            "critical_failures": 0,
        }

        for result in results:
            if result.success:
                parts.append(f"## {result.tool_name}\n{result.value}")
                degradation_report["succeeded"] += 1
            else:
                note = result.context_note()
                if note:
                    parts.append(note)
                degradation_report["failed"] += 1
                if result.fallback_used:
                    degradation_report["fallback_used"] += 1
                if result.criticality == "critical":
                    degradation_report["critical_failures"] += 1

        return "\n\n".join(parts), degradation_report
```

## Solution 5: Degradation Policy Enforcer

```python
from typing import List


class DegradationPolicyEnforcer:
    """
    Evaluates a set of tool results against a minimum quality policy.
    Blocks response emission if too many important tools failed.
    """

    def __init__(
        self,
        max_important_failures: int = 2,
        min_success_rate: float = 0.5,
    ):
        self._max_important = max_important_failures
        self._min_success_rate = min_success_rate

    def evaluate(self, results: List[DegradedToolResult]) -> dict:
        if not results:
            return {"proceed": True, "reason": "no tools"}

        total = len(results)
        succeeded = sum(1 for r in results if r.success)
        important_failures = sum(
            1 for r in results
            if not r.success and r.criticality == "important"
        )
        success_rate = succeeded / total

        if important_failures > self._max_important:
            return {
                "proceed": False,
                "reason": f"{important_failures} important tools failed (max {self._max_important})",
                "success_rate": success_rate,
            }
        if success_rate < self._min_success_rate:
            return {
                "proceed": False,
                "reason": f"success rate {success_rate:.0%} below minimum {self._min_success_rate:.0%}",
                "success_rate": success_rate,
            }
        return {"proceed": True, "success_rate": success_rate}
```

## Solution 6: Graceful Degradation Monitor

```python
import time
from threading import Lock
from typing import List


class GracefulDegradationMonitor:
    """
    Tracks degradation events over time to surface which tools
    are failing most often and whether graceful degradation is
    masking a systematic outage.
    """

    def __init__(self):
        self._records: List[dict] = []
        self._lock = Lock()

    def record(self, results: List[DegradedToolResult]) -> None:
        with self._lock:
            for r in results:
                if not r.success:
                    self._records.append({
                        "ts": time.time(),
                        "tool_name": r.tool_name,
                        "criticality": r.criticality,
                        "error": r.error,
                        "fallback_used": r.fallback_used,
                    })
            if len(self._records) > 50000:
                self._records = self._records[-50000:]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [r for r in self._records if r["ts"] >= cutoff]

        tool_counts: dict = {}
        for r in recent:
            tool_counts[r["tool_name"]] = tool_counts.get(r["tool_name"], 0) + 1

        return {
            "window_seconds": window_seconds,
            "total_failures": len(recent),
            "by_tool": dict(sorted(tool_counts.items(), key=lambda x: -x[1])),
        }
```

## Comparison

| Approach | Criticality Classification | Timeout Per Tool | Fallback Value | Context Injection | Policy Gate |
|---|---|---|---|---|---|
| GracefulToolDispatcher | Yes (4 levels) | Yes | Yes | No | No |
| PartialContextAssembler | No | No | No | Yes | No |
| DegradationPolicyEnforcer | No | No | No | No | Yes |
| GracefulDegradationMonitor | No | No | No | No | No (retrospective) |

**Best for production**: Mark weather, news, recommendations, and any third-party enrichment APIs as `ENRICHMENT` criticality with a `timeout_seconds=3.0` — these must never block a response. Mark identity, permissions, and billing lookups as `CRITICAL`. Set `failure_message` on `IMPORTANT` tools to a user-readable note (e.g., "Recent pricing unavailable — showing last cached values") so the LLM can include it naturally in the response. Monitor `GracefulDegradationMonitor.summary()`: if any single tool appears in more than 20% of failures over an hour, it has likely gone down and the silence from graceful degradation is masking an outage that needs an on-call page.
