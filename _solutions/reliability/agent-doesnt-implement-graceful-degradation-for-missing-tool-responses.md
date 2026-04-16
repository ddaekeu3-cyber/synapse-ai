---
title: "Agent Doesn't Implement Graceful Degradation for Missing Tool Responses"
description: "Agents that treat every tool failure as fatal crash the session when a non-critical tool is unavailable — a search enrichment failure aborts the entire response, a weather API timeout halts the conversation. Implement graceful degradation that classifies tool criticality, substitutes cached or default responses for non-critical failures, and continues session execution with a reduced feature set rather than a hard stop."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-graceful-degradation-for-missing-tool-responses
tags: [graceful-degradation, tool-failure, fallback, resilience, fault-tolerance, partial-response]
symptoms:
  - "Agent returns a hard error when an enrichment API is temporarily unavailable"
  - "Session aborts entirely when one of five parallel tool calls fails"
  - "No fallback path — the agent cannot answer without every tool succeeding"
  - "Non-critical tool failures are indistinguishable from critical ones in error handling"
  - "Users receive 'service unavailable' for queries that could be answered partially"
---

## Why This Happens

The simplest tool call implementation is `result = await tool()` without any fallback. When the tool raises, the exception propagates up and the session handler returns an error. This is correct for critical tools (without the database, there is no answer) but wrong for enrichment tools (without trending topics, the answer is still valid — just less enriched). Graceful degradation requires classifying tool criticality, wrapping non-critical calls with fallback logic, and assembling partial responses from whatever succeeded.

## Solution 1: Tool Call Result

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ToolCriticality(str, Enum):
    CRITICAL = "critical"       # session must abort if this fails
    HIGH = "high"               # significant quality loss without it
    LOW = "low"                 # minor enrichment; session continues without it
    OPTIONAL = "optional"       # best-effort; silently omitted on failure


class ToolCallOutcome(str, Enum):
    SUCCESS = "success"
    FAILED_CRITICAL = "failed_critical"
    FAILED_DEGRADED = "failed_degraded"     # failed but session continues
    SUBSTITUTED = "substituted"             # fallback value used
    SKIPPED = "skipped"                     # tool not attempted (already degraded)


@dataclass
class ToolCallResult:
    tool_name: str
    criticality: ToolCriticality
    outcome: ToolCallOutcome
    value: Any = None
    error: Optional[str] = None
    fallback_used: bool = False
    latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)

    def succeeded(self) -> bool:
        return self.outcome == ToolCallOutcome.SUCCESS

    def available(self) -> bool:
        return self.outcome in (ToolCallOutcome.SUCCESS, ToolCallOutcome.SUBSTITUTED)
```

## Solution 2: Tool Criticality Registry

```python
from typing import Any, Callable, Dict, Optional


@dataclass
class ToolDegradationPolicy:
    criticality: ToolCriticality
    fallback_value: Any = None          # static fallback
    fallback_fn: Optional[Callable] = None   # async callable fallback
    max_retries: int = 0
    timeout_seconds: float = 10.0
    suppress_error_log: bool = False


class ToolCriticalityRegistry:
    """
    Stores degradation policies for each registered tool.
    Tools not in the registry default to CRITICAL criticality.
    """

    def __init__(self):
        self._policies: Dict[str, ToolDegradationPolicy] = {}

    def register(self, tool_name: str, policy: ToolDegradationPolicy) -> None:
        self._policies[tool_name] = policy

    def get(self, tool_name: str) -> ToolDegradationPolicy:
        return self._policies.get(
            tool_name,
            ToolDegradationPolicy(criticality=ToolCriticality.CRITICAL),
        )

    def is_critical(self, tool_name: str) -> bool:
        return self.get(tool_name).criticality == ToolCriticality.CRITICAL

    def all_policies(self) -> Dict[str, ToolDegradationPolicy]:
        return dict(self._policies)
```

## Solution 3: Graceful Tool Executor

```python
import asyncio
import time
from typing import Any, Callable


class GracefulToolExecutor:
    """
    Wraps a tool call with retry, timeout, and fallback logic.
    On failure of a non-critical tool, returns a ToolCallResult with
    the fallback value and outcome=SUBSTITUTED instead of raising.
    On failure of a CRITICAL tool, re-raises so the session can abort.
    """

    def __init__(self, registry: ToolCriticalityRegistry):
        self._registry = registry

    async def call(
        self,
        tool_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> ToolCallResult:
        policy = self._registry.get(tool_name)
        start = time.time()

        for attempt in range(policy.max_retries + 1):
            try:
                value = await asyncio.wait_for(
                    tool_fn(*args, **kwargs),
                    timeout=policy.timeout_seconds,
                )
                latency = (time.time() - start) * 1000
                return ToolCallResult(
                    tool_name=tool_name,
                    criticality=policy.criticality,
                    outcome=ToolCallOutcome.SUCCESS,
                    value=value,
                    latency_ms=round(latency, 2),
                )
            except (asyncio.TimeoutError, Exception) as exc:
                if attempt < policy.max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue

                latency = (time.time() - start) * 1000
                error_str = str(exc)[:200]

                if policy.criticality == ToolCriticality.CRITICAL:
                    raise

                # Non-critical: resolve fallback
                fallback_value = policy.fallback_value
                fallback_used = False
                if policy.fallback_fn is not None:
                    try:
                        fallback_value = await policy.fallback_fn(*args, **kwargs)
                        fallback_used = True
                    except Exception:
                        fallback_value = policy.fallback_value

                return ToolCallResult(
                    tool_name=tool_name,
                    criticality=policy.criticality,
                    outcome=ToolCallOutcome.SUBSTITUTED,
                    value=fallback_value,
                    error=error_str,
                    fallback_used=True,
                    latency_ms=round(latency, 2),
                )
        # unreachable
        raise RuntimeError("GracefulToolExecutor loop exited without return")
```

## Solution 4: Degradation-Aware Session Runner

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class SessionDegradationState:
    failed_critical: List[str] = field(default_factory=list)
    failed_high: List[str] = field(default_factory=list)
    substituted_tools: List[str] = field(default_factory=list)
    is_aborted: bool = False

    def has_quality_loss(self) -> bool:
        return bool(self.failed_high or self.substituted_tools)

    def degradation_note(self) -> Optional[str]:
        if self.is_aborted:
            return f"Session aborted: critical tool failures: {self.failed_critical}"
        parts = []
        if self.failed_high:
            parts.append(f"reduced quality (tools unavailable: {self.failed_high})")
        if self.substituted_tools:
            parts.append(f"fallback values used for: {self.substituted_tools}")
        return "; ".join(parts) if parts else None


class DegradationAwareSessionRunner:
    """
    Executes a list of tool calls and accumulates results.
    Aborts on the first CRITICAL failure.
    Continues and notes quality degradation for HIGH/LOW/OPTIONAL failures.
    """

    def __init__(self, executor: GracefulToolExecutor):
        self._executor = executor

    async def run_parallel(
        self,
        calls: List[Tuple[str, Callable, tuple, dict]],
    ) -> Tuple[Dict[str, ToolCallResult], SessionDegradationState]:
        """
        calls: list of (tool_name, tool_fn, args, kwargs)
        Returns all results and the degradation state.
        """
        state = SessionDegradationState()
        tasks = {
            tool_name: asyncio.create_task(
                self._executor.call(tool_name, fn, *args, **kwargs)
            )
            for tool_name, fn, args, kwargs in calls
        }

        results: Dict[str, ToolCallResult] = {}
        for tool_name, task in tasks.items():
            try:
                result = await task
                results[tool_name] = result
                if result.outcome == ToolCallOutcome.SUBSTITUTED:
                    state.substituted_tools.append(tool_name)
                    if result.criticality == ToolCriticality.HIGH:
                        state.failed_high.append(tool_name)
            except Exception as exc:
                # Only CRITICAL tools re-raise from executor
                state.failed_critical.append(tool_name)
                state.is_aborted = True
                results[tool_name] = ToolCallResult(
                    tool_name=tool_name,
                    criticality=ToolCriticality.CRITICAL,
                    outcome=ToolCallOutcome.FAILED_CRITICAL,
                    error=str(exc)[:200],
                )

        return results, state
```

## Solution 5: Partial Response Assembler

```python
from typing import Any, Dict, List, Optional


class PartialResponseAssembler:
    """
    Assembles a final response from tool results that may be partial.
    Skips unavailable tool slots and annotates the response with
    a degradation notice when quality is reduced.
    """

    def __init__(self, degradation_notice_template: str = "[Note: {note}]"):
        self._notice_template = degradation_notice_template

    def assemble(
        self,
        core_response: str,
        enrichments: Dict[str, ToolCallResult],
        degradation_state: SessionDegradationState,
    ) -> dict:
        """
        core_response: the base answer (from critical tools that succeeded)
        enrichments: results from HIGH/LOW/OPTIONAL tools
        Returns: assembled response dict with quality metadata
        """
        available_enrichments = {
            name: result.value
            for name, result in enrichments.items()
            if result.available() and result.value is not None
        }
        missing_enrichments = [
            name for name, result in enrichments.items()
            if not result.available()
        ]

        response_text = core_response
        notice = degradation_state.degradation_note()
        if notice:
            response_text += "\n\n" + self._notice_template.format(note=notice)

        return {
            "response": response_text,
            "enrichments": available_enrichments,
            "missing_enrichments": missing_enrichments,
            "quality": "full" if not degradation_state.has_quality_loss() else "degraded",
            "aborted": degradation_state.is_aborted,
        }
```

## Solution 6: Degradation Metrics Collector

```python
import time
from collections import defaultdict
from typing import Dict, List


class DegradationMetricsCollector:
    """
    Accumulates degradation events across sessions.
    Identifies which tools degrade most often and whether
    degradation rate is increasing (indicating worsening reliability).
    """

    def __init__(self, window_seconds: float = 3600.0):
        self._window = window_seconds
        self._events: List[dict] = []

    def record(
        self,
        state: SessionDegradationState,
        results: Dict[str, "ToolCallResult"],
    ) -> None:
        ts = time.time()
        for tool_name in state.substituted_tools + state.failed_critical:
            result = results.get(tool_name)
            self._events.append({
                "ts": ts,
                "tool_name": tool_name,
                "criticality": result.criticality.value if result else "unknown",
                "outcome": result.outcome.value if result else "unknown",
            })

    def _trim(self) -> None:
        cutoff = time.time() - self._window
        self._events = [e for e in self._events if e["ts"] >= cutoff]

    def summary(self) -> dict:
        self._trim()
        by_tool: Dict[str, int] = defaultdict(int)
        for e in self._events:
            by_tool[e["tool_name"]] += 1

        return {
            "window_seconds": self._window,
            "total_degradation_events": len(self._events),
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])),
            "most_degraded_tool": max(by_tool, key=by_tool.get) if by_tool else None,
        }
```

## Comparison

| Approach | Criticality Classification | Fallback Execution | Parallel Support | Response Assembly | Metrics |
|---|---|---|---|---|---|
| ToolCriticalityRegistry | Yes | No | No | No | No |
| GracefulToolExecutor | Via registry | Yes (static + fn) | No | No | No |
| DegradationAwareSessionRunner | Via executor | Via executor | Yes | No | No |
| PartialResponseAssembler | No | No | No | Yes | No |
| DegradationMetricsCollector | No | No | No | No | Yes |

**Best for production**: Register every tool with an explicit `ToolDegradationPolicy`. Default non-registered tools to `CRITICAL` so new tools are safe until explicitly classified. Set `fallback_value=None` for enrichment tools and check `result.available()` before using the value in response assembly. Mark weather, trending-topic, and personalization tools as `OPTIONAL` — their absence should never surface as an error to the user. Monitor `DegradationMetricsCollector.summary()` to find tools that degrade frequently: if a tool's degradation rate exceeds 5%, it either needs a better fallback or should be removed from the critical path entirely.
