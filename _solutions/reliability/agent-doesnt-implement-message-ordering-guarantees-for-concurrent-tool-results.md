---
title: "Agent Doesn't Implement Message Ordering Guarantees for Concurrent Tool Results"
description: "Agents that dispatch multiple tool calls concurrently and collect results as they arrive inject them into the conversation in completion order — which may differ from dispatch order. LLM providers require tool results to appear in the same order as the corresponding tool_use blocks in the assistant message. Out-of-order results cause API validation errors or silent misattribution where the LLM associates a result with the wrong tool call ID. Implement ordering guarantees that sort concurrent results back to their original dispatch order before injecting them."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-message-ordering-guarantees-for-concurrent-tool-results
tags: [message-ordering, concurrent-tools, tool-result-ordering, race-condition, api-compliance, result-sequencing]
symptoms:
  - "API returns 'tool_result order does not match tool_use order' validation errors"
  - "LLM attributes a search result to the wrong tool call when parallel calls complete out of order"
  - "Intermittent failures in multi-tool calls that only occur when one tool is slower than another"
  - "Tool results injected in arrival order rather than request order"
  - "No mechanism to correlate concurrent results back to their originating requests"
---

## Why This Happens

When an agent fires N tool calls concurrently with `asyncio.gather()` or similar, the results arrive in the order determined by each tool's execution time — not the order in which the calls were dispatched. Anthropic's Messages API requires that `tool_result` content blocks appear in the same sequence as the `tool_use` blocks in the preceding assistant message. If the agent naively appends results as they complete, the sequence will be scrambled whenever tools have different latencies. The fix is straightforward: track dispatch order by index, collect results into a pre-sized array keyed by index, and assemble the final message only after all results are present.

## Solution 1: Ordered Tool Dispatch Record

```python
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolDispatch:
    dispatch_index: int           # position in the dispatch batch (0-based)
    tool_use_id: str              # the ID from the assistant's tool_use block
    tool_name: str
    tool_input: dict
    dispatched_at: float = field(default_factory=time.time)


@dataclass
class ToolResult:
    dispatch_index: int
    tool_use_id: str
    tool_name: str
    content: Any                  # the result value
    is_error: bool = False
    error_message: str = ""
    completed_at: float = field(default_factory=time.time)
    latency_ms: float = 0.0

    def to_api_block(self) -> dict:
        """Serialize to Anthropic tool_result content block format."""
        block: dict = {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
        }
        if self.is_error:
            block["is_error"] = True
            block["content"] = self.error_message or "tool execution failed"
        else:
            content = self.content
            if isinstance(content, str):
                block["content"] = content
            else:
                import json
                block["content"] = json.dumps(content)
        return block
```

## Solution 2: Ordered Result Collector

```python
import asyncio
from typing import Dict, List, Optional


class OrderedResultCollector:
    """
    Collects tool results in arbitrary arrival order but preserves
    dispatch order when producing the final ordered result list.
    """

    def __init__(self, expected_count: int):
        self._expected = expected_count
        self._results: Dict[int, ToolResult] = {}
        self._event = asyncio.Event()

    def add(self, result: ToolResult) -> None:
        self._results[result.dispatch_index] = result
        if len(self._results) >= self._expected:
            self._event.set()

    async def wait_all(self, timeout_seconds: float = 60.0) -> List[ToolResult]:
        """
        Waits for all results and returns them sorted by dispatch_index.
        Raises TimeoutError if not all results arrive within the timeout.
        """
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            missing = [
                i for i in range(self._expected)
                if i not in self._results
            ]
            raise ToolResultTimeoutError(missing_indices=missing)

        return [self._results[i] for i in sorted(self._results.keys())]

    def partial_results(self) -> List[ToolResult]:
        """Returns whatever results have arrived so far, in order."""
        return [self._results[i] for i in sorted(self._results.keys())]

    def is_complete(self) -> bool:
        return len(self._results) >= self._expected


class ToolResultTimeoutError(Exception):
    def __init__(self, missing_indices: List[int]):
        super().__init__(f"tool results timed out for dispatch indices: {missing_indices}")
        self.missing_indices = missing_indices
```

## Solution 3: Concurrent Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class ConcurrentOrderedToolDispatcher:
    """
    Dispatches multiple tool calls concurrently and collects results
    in dispatch order, not arrival order.
    """

    def __init__(
        self,
        tool_registry: Dict[str, Callable],
        timeout_seconds: float = 60.0,
    ):
        self._registry = tool_registry
        self._timeout = timeout_seconds
        self._total_dispatches = 0

    async def dispatch_all(
        self, dispatches: List[ToolDispatch]
    ) -> List[ToolResult]:
        """
        Fires all tool calls concurrently and returns results in dispatch order.
        """
        collector = OrderedResultCollector(expected_count=len(dispatches))
        self._total_dispatches += len(dispatches)

        async def _run_one(dispatch: ToolDispatch) -> None:
            start = time.time()
            tool_fn = self._registry.get(dispatch.tool_name)
            if tool_fn is None:
                collector.add(ToolResult(
                    dispatch_index=dispatch.dispatch_index,
                    tool_use_id=dispatch.tool_use_id,
                    tool_name=dispatch.tool_name,
                    content=None,
                    is_error=True,
                    error_message=f"tool '{dispatch.tool_name}' not registered",
                    latency_ms=round((time.time() - start) * 1000, 2),
                ))
                return
            try:
                result_content = await asyncio.wait_for(
                    tool_fn(**dispatch.tool_input),
                    timeout=self._timeout,
                )
                collector.add(ToolResult(
                    dispatch_index=dispatch.dispatch_index,
                    tool_use_id=dispatch.tool_use_id,
                    tool_name=dispatch.tool_name,
                    content=result_content,
                    latency_ms=round((time.time() - start) * 1000, 2),
                ))
            except Exception as exc:
                collector.add(ToolResult(
                    dispatch_index=dispatch.dispatch_index,
                    tool_use_id=dispatch.tool_use_id,
                    tool_name=dispatch.tool_name,
                    content=None,
                    is_error=True,
                    error_message=str(exc),
                    latency_ms=round((time.time() - start) * 1000, 2),
                ))

        await asyncio.gather(*[_run_one(d) for d in dispatches])
        return await collector.wait_all(timeout_seconds=self._timeout)
```

## Solution 4: API-Compliant Result Message Builder

```python
from typing import List


class APICompliantResultMessageBuilder:
    """
    Converts an ordered list of ToolResult objects into a single
    user-role message with tool_result content blocks in the correct order.
    Compatible with Anthropic Messages API format.
    """

    def build(self, ordered_results: List[ToolResult]) -> dict:
        """
        Returns a message dict ready to append to the conversation.
        """
        content_blocks = [result.to_api_block() for result in ordered_results]
        return {
            "role": "user",
            "content": content_blocks,
        }

    def validate_order(
        self,
        dispatches: List[ToolDispatch],
        results: List[ToolResult],
    ) -> bool:
        """
        Verifies that results are in the same order as dispatches.
        """
        if len(dispatches) != len(results):
            return False
        return all(
            d.tool_use_id == r.tool_use_id
            for d, r in zip(dispatches, results)
        )
```

## Solution 5: Ordering Violation Detector

```python
import time
from typing import List


class OrderingViolationDetector:
    """
    Detects and logs cases where tool results arrived out of order,
    quantifying how frequently ordering would have been violated
    without the collector.
    """

    def __init__(self):
        self._violations: list = []

    def check(self, results: List[ToolResult]) -> int:
        """
        Returns the number of out-of-order arrivals that were corrected.
        """
        if len(results) < 2:
            return 0
        arrival_order = sorted(
            range(len(results)),
            key=lambda i: results[i].completed_at,
        )
        dispatch_order = list(range(len(results)))
        violations = sum(
            1 for a, d in zip(arrival_order, dispatch_order) if a != d
        )
        if violations > 0:
            self._violations.append({
                "ts": time.time(),
                "batch_size": len(results),
                "violations": violations,
            })
        return violations

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [v for v in self._violations if v["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "batches_with_violations": len(recent),
            "total_violations_corrected": sum(v["violations"] for v in recent),
        }
```

## Solution 6: Ordered Dispatch Dashboard

```python
import time


class OrderedDispatchDashboard:
    """
    Combines dispatcher stats and violation detection into a single view.
    """

    def __init__(
        self,
        dispatcher: ConcurrentOrderedToolDispatcher,
        violation_detector: OrderingViolationDetector,
    ):
        self._dispatcher = dispatcher
        self._detector = violation_detector

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "total_dispatches": self._dispatcher._total_dispatches,
            "ordering_violations": self._detector.summary(window_seconds),
        }
```

## Comparison

| Approach | Dispatch Tracking | Ordered Collection | API Message Build | Violation Detection | Dashboard |
|---|---|---|---|---|---|
| ToolDispatch + ToolResult | Yes | No | No | No | No |
| OrderedResultCollector | No | Yes (index-keyed) | No | No | No |
| ConcurrentOrderedToolDispatcher | Yes | Via collector | No | No | No |
| APICompliantResultMessageBuilder | No | No | Yes | No | No |
| OrderingViolationDetector | No | No | No | Yes | No |
| OrderedDispatchDashboard | No | No | No | No | Yes |

**Best for production**: Replace every `asyncio.gather()` that collects tool results with `ConcurrentOrderedToolDispatcher.dispatch_all()` — the API compliance requirement makes this non-negotiable for Anthropic's Messages API. Use `OrderingViolationDetector.check()` during testing to quantify how often results would arrive out of order in your workload — if violations are rare (< 5% of batches), the ordering overhead is dominated by tool latency variance and is worth keeping. Set `timeout_seconds` per dispatcher to match the slowest tool in each batch category; do not use a single global timeout that is sized for the worst-case tool and applied to all batches.
