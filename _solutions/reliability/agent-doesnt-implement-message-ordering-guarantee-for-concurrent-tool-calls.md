---
title: "Agent Doesn't Implement Message Ordering Guarantee for Concurrent Tool Calls"
description: "Agents that execute multiple tool calls concurrently and inject their results into the LLM context in completion order rather than submission order produce non-deterministic prompts — the same parallel tool calls may yield different context orderings on different runs, making behavior hard to reproduce and test. Implement message ordering guarantees that preserve submission order when assembling concurrent tool results into the LLM context."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-message-ordering-guarantee-for-concurrent-tool-calls
tags: [message-ordering, concurrent-tools, deterministic-context, tool-result-ordering, parallel-execution, reproducibility]
symptoms:
  - "Same parallel tool calls produce different context orderings on different runs"
  - "LLM reasoning changes based on which tool result happened to arrive first"
  - "Test fixtures fail intermittently because tool result order is non-deterministic"
  - "No sequence number on tool calls — results assembled in arrival order"
  - "Debugging concurrent tool execution impossible because context ordering varies"
---

## Why This Happens

Concurrent tool execution with `asyncio.gather()` returns results in submission order, but agents that process results as they arrive (using `asyncio.as_completed()`) or that append results to a shared list from callbacks inject them in completion order. Completion order depends on network latency, upstream API response times, and system scheduling — it is non-deterministic. The LLM context should always reflect the deterministic submission order so that the agent's behavior is reproducible given the same inputs. Ordering guarantees require assigning sequence numbers at submission time and sorting results by sequence number before context injection.

## Solution 1: Ordered Tool Call

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ToolCallState(str, Enum):
    SUBMITTED = "submitted"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class OrderedToolCall:
    sequence: int                # submission order (0-based, assigned at submission)
    tool_name: str
    args: dict
    call_id: str = ""
    state: ToolCallState = ToolCallState.SUBMITTED
    result: Any = None
    error: str = ""
    submitted_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    latency_ms: Optional[float] = None

    def mark_complete(self, result: Any) -> None:
        self.state = ToolCallState.COMPLETED
        self.result = result
        self.completed_at = time.time()
        self.latency_ms = round((self.completed_at - self.submitted_at) * 1000, 2)

    def mark_failed(self, error: str) -> None:
        self.state = ToolCallState.FAILED
        self.error = error
        self.completed_at = time.time()
        self.latency_ms = round((self.completed_at - self.submitted_at) * 1000, 2)
```

## Solution 2: Concurrent Tool Execution Coordinator

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class ConcurrentToolExecutionCoordinator:
    """
    Executes multiple tool calls concurrently while preserving submission order
    in the returned results. Uses asyncio.gather() which returns results in
    the order tasks were submitted, not completion order.
    """

    def __init__(
        self,
        tool_dispatch_fn: Callable,    # async (tool_name, args) -> result
        max_concurrent: int = 10,
        per_tool_timeout_s: float = 30.0,
    ):
        self._dispatch = tool_dispatch_fn
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._timeout = per_tool_timeout_s
        self._total_batches = 0
        self._total_calls = 0
        self._ordering_violations = 0

    async def _execute_one(self, call: OrderedToolCall) -> OrderedToolCall:
        async with self._semaphore:
            call.state = ToolCallState.RUNNING
            try:
                result = await asyncio.wait_for(
                    self._dispatch(call.tool_name, call.args),
                    timeout=self._timeout,
                )
                call.mark_complete(result)
            except asyncio.TimeoutError:
                call.mark_failed(f"timeout after {self._timeout}s")
            except Exception as exc:
                call.mark_failed(str(exc)[:300])
        return call

    async def execute_batch(
        self,
        calls: List[OrderedToolCall],
    ) -> List[OrderedToolCall]:
        """
        Executes all calls concurrently and returns results in SUBMISSION ORDER.
        Sequence numbers are used to verify ordering is preserved.
        """
        if not calls:
            return []

        self._total_batches += 1
        self._total_calls += len(calls)

        # Assign sequence numbers if not already assigned
        for i, call in enumerate(calls):
            if call.sequence == 0 and i > 0:
                call.sequence = i

        # asyncio.gather preserves order of its awaitables
        tasks = [self._execute_one(call) for call in calls]
        results = await asyncio.gather(*tasks, return_exceptions=False)

        # Verify ordering (results should match submission order)
        ordered = sorted(results, key=lambda c: c.sequence)
        actual_order = [c.sequence for c in results]
        expected_order = [c.sequence for c in ordered]
        if actual_order != expected_order:
            self._ordering_violations += 1

        return ordered   # always return in sequence order

    def stats(self) -> dict:
        return {
            "total_batches": self._total_batches,
            "total_calls": self._total_calls,
            "ordering_violations_detected": self._ordering_violations,
        }
```

## Solution 3: Ordered Result Assembler

```python
from typing import Any, Dict, List, Optional


class OrderedToolResultAssembler:
    """
    Assembles tool results into LLM context messages in submission order.
    Handles failed calls with error placeholders rather than omitting them,
    so the context structure is predictable regardless of failure patterns.
    """

    def __init__(
        self,
        include_failed: bool = True,
        error_placeholder: str = "[Tool call failed: {error}]",
    ):
        self._include_failed = include_failed
        self._error_placeholder = error_placeholder

    def assemble(
        self,
        calls: List[OrderedToolCall],
        format_fn: Optional[callable] = None,
    ) -> List[dict]:
        """
        Returns list of context message dicts in submission order.
        format_fn: optional (call) -> dict for custom message format.
        """
        # Ensure submission order
        sorted_calls = sorted(calls, key=lambda c: c.sequence)
        messages = []

        for call in sorted_calls:
            if call.state == ToolCallState.COMPLETED:
                if format_fn:
                    messages.append(format_fn(call))
                else:
                    messages.append({
                        "role": "tool",
                        "tool_name": call.tool_name,
                        "call_id": call.call_id,
                        "content": str(call.result)[:8000],
                        "sequence": call.sequence,
                    })
            elif self._include_failed:
                messages.append({
                    "role": "tool",
                    "tool_name": call.tool_name,
                    "call_id": call.call_id,
                    "content": self._error_placeholder.format(error=call.error[:200]),
                    "sequence": call.sequence,
                    "failed": True,
                })
        return messages
```

## Solution 4: Determinism Verifier

```python
import hashlib
import json
from typing import List


class ToolBatchDeterminismVerifier:
    """
    Computes a fingerprint for a batch of ordered tool results.
    Two batches with identical tool names, args, and results in the same
    submission order should produce identical fingerprints — verifying
    that context assembly is deterministic.
    """

    @staticmethod
    def fingerprint(calls: List[OrderedToolCall]) -> str:
        sorted_calls = sorted(calls, key=lambda c: c.sequence)
        data = [
            {
                "seq": c.sequence,
                "tool": c.tool_name,
                "state": c.state.value,
                "result_hash": hashlib.sha256(str(c.result).encode()).hexdigest()[:16]
                if c.result is not None else None,
                "error": c.error[:50] if c.error else None,
            }
            for c in sorted_calls
        ]
        return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:24]

    @staticmethod
    def verify_order(calls: List[OrderedToolCall]) -> tuple:
        """Returns (is_ordered, violations) where violations list out-of-order pairs."""
        sorted_calls = sorted(calls, key=lambda c: c.sequence)
        violations = []
        for i in range(1, len(sorted_calls)):
            if sorted_calls[i].sequence <= sorted_calls[i-1].sequence:
                violations.append({
                    "expected_seq": sorted_calls[i-1].sequence + 1,
                    "actual_seq": sorted_calls[i].sequence,
                    "tool": sorted_calls[i].tool_name,
                })
        return len(violations) == 0, violations
```

## Solution 5: Ordering Audit Logger

```python
import time
from typing import List


class ToolOrderingAuditLogger:
    """
    Records tool batch execution events with ordering verification results.
    Surfaces batches where completion order differed from submission order.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[dict] = []
        self._max = max_records

    def record(
        self,
        calls: List[OrderedToolCall],
        batch_fingerprint: str,
        ordering_ok: bool,
        session_id: str = "",
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)

        completion_order = sorted(
            [c for c in calls if c.completed_at is not None],
            key=lambda c: c.completed_at,
        )
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "batch_size": len(calls),
            "fingerprint": batch_fingerprint,
            "ordering_ok": ordering_ok,
            "submission_order": [c.tool_name for c in sorted(calls, key=lambda c: c.sequence)],
            "completion_order": [c.tool_name for c in completion_order],
            "failures": sum(1 for c in calls if c.state == ToolCallState.FAILED),
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "batches": 0}
        ordering_issues = sum(1 for r in recent if not r["ordering_ok"])
        return {
            "window_seconds": window_seconds,
            "batches": len(recent),
            "ordering_issues": ordering_issues,
            "ordering_issue_rate": round(ordering_issues / max(len(recent), 1), 4),
            "mean_batch_size": round(sum(r["batch_size"] for r in recent) / len(recent), 2),
            "total_failures": sum(r["failures"] for r in recent),
        }
```

## Solution 6: Concurrent Tool Execution Dashboard

```python
import time


class ConcurrentToolExecutionDashboard:
    """
    Combines coordinator stats, ordering verifier results, and audit log
    into a single operational view.
    """

    def __init__(
        self,
        coordinator: ConcurrentToolExecutionCoordinator,
        verifier: ToolBatchDeterminismVerifier,
        audit_logger: ToolOrderingAuditLogger,
    ):
        self._coordinator = coordinator
        self._verifier = verifier
        self._audit = audit_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "coordinator_stats": self._coordinator.stats(),
            "audit_summary_last_hour": self._audit.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Submission-Order Return | Sequence Numbers | Error Placeholders | Fingerprinting | Ordering Audit |
|---|---|---|---|---|---|
| ConcurrentToolExecutionCoordinator | Yes (gather preserves) | Yes | No | No | No |
| OrderedToolResultAssembler | Yes (sort by seq) | Via calls | Yes | No | No |
| ToolBatchDeterminismVerifier | Via sort | No | No | Yes | No |
| ToolOrderingAuditLogger | No | No | No | Via verifier | Yes |
| ConcurrentToolExecutionDashboard | No | No | No | No | Via logger |

**Best for production**: Always use `asyncio.gather()` rather than `asyncio.as_completed()` when ordering matters — `gather` returns results in submission order; `as_completed` returns them in completion order. Assign sequence numbers at submission time before any concurrent execution begins — a sequence number based on list position is sufficient. Include error placeholders for failed tool calls rather than omitting them — a context message with a predictable structure at position N is easier for the LLM to reason about than a context where position N is missing. Monitor `ordering_issue_rate` — any non-zero value indicates that somewhere in the pipeline results are being accumulated out of order.
