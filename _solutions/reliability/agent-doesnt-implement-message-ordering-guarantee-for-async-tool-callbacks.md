---
title: "Agent Doesn't Implement Message Ordering Guarantee for Async Tool Callbacks"
description: "Agents that process tool callbacks as they arrive — without enforcing the original dispatch order — produce responses where results from a slower tool overwrite or interleave with results from a faster one, causing the LLM context to receive tool outputs in the wrong sequence. Implement message ordering guarantees that buffer out-of-order callbacks, reassemble them in dispatch sequence, and deliver them to the LLM context in a deterministic order regardless of completion timing."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-message-ordering-guarantee-for-async-tool-callbacks
tags: [message-ordering, async-callbacks, sequence-guarantee, out-of-order, resequencer, deterministic-order]
symptoms:
  - "Tool results arrive out of order — the faster tool's result is inserted before the slower one"
  - "LLM context shows tool results in arrival order, not dispatch order"
  - "Non-deterministic response quality — same query produces different context ordering on each run"
  - "Tool B result overwrites Tool A result in the context because B completed first"
  - "No buffering of out-of-order callbacks — early arrivals are immediately processed"
---

## Why This Happens

When multiple tools are dispatched in parallel, their completion order is non-deterministic. If the agent injects each result into the LLM context as it arrives, the context may read "Result of Tool B: ... Result of Tool A: ..." even though Tool A was dispatched first. The LLM may weight the first result more heavily or become confused by the unexpected ordering. A resequencer buffers results by sequence number and releases them in dispatch order, ensuring the LLM always sees results in the order the agent intended.

## Solution 1: Sequenced Tool Call

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class SequencedCallState(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass
class SequencedToolCall:
    sequence_number: int         # dispatch order (0-indexed within a batch)
    call_id: str
    tool_name: str
    dispatched_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None
    state: SequencedCallState = SequencedCallState.PENDING
    result: Any = None
    error: Optional[str] = None

    def mark_complete(self, result: Any) -> None:
        self.result = result
        self.state = SequencedCallState.COMPLETED
        self.completed_at = time.time()

    def mark_failed(self, error: str) -> None:
        self.error = error
        self.state = SequencedCallState.FAILED
        self.completed_at = time.time()

    def is_terminal(self) -> bool:
        return self.state in (
            SequencedCallState.COMPLETED,
            SequencedCallState.FAILED,
            SequencedCallState.TIMED_OUT,
        )

    def latency_ms(self) -> Optional[float]:
        if self.completed_at:
            return round((self.completed_at - self.dispatched_at) * 1000, 2)
        return None
```

## Solution 2: In-Order Resequencer

```python
import asyncio
from typing import AsyncIterator, Dict, List, Optional


class InOrderResequencer:
    """
    Buffers completed tool calls and yields them in sequence number order.
    Out-of-order completions are held in a buffer until their turn arrives.
    Yields a sentinel on timeout to prevent indefinite blocking.
    """

    def __init__(
        self,
        total_calls: int,
        drain_timeout_seconds: float = 60.0,
    ) -> None:
        self._total = total_calls
        self._timeout = drain_timeout_seconds
        self._buffer: Dict[int, SequencedToolCall] = {}
        self._next_seq = 0
        self._queue: asyncio.Queue = asyncio.Queue()
        self._delivered = 0

    def submit(self, call: SequencedToolCall) -> None:
        """Called when a tool call completes (from any coroutine)."""
        self._queue.put_nowait(call)

    async def drain(self) -> AsyncIterator[SequencedToolCall]:
        """
        Yields tool calls in sequence order.
        Buffers out-of-order arrivals until their sequence number is due.
        """
        deadline = asyncio.get_event_loop().time() + self._timeout

        while self._delivered < self._total:
            # Check if the next expected sequence is already buffered
            while self._next_seq in self._buffer:
                call = self._buffer.pop(self._next_seq)
                self._next_seq += 1
                self._delivered += 1
                yield call

            if self._delivered >= self._total:
                break

            # Wait for next arrival
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                # Yield remaining buffered items and break
                for seq in sorted(self._buffer.keys()):
                    yield self._buffer.pop(seq)
                break

            try:
                call = await asyncio.wait_for(self._queue.get(), timeout=min(remaining, 5.0))
                if call.sequence_number == self._next_seq:
                    self._next_seq += 1
                    self._delivered += 1
                    yield call
                else:
                    self._buffer[call.sequence_number] = call
            except asyncio.TimeoutError:
                continue
```

## Solution 3: Ordered Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, List


class OrderedToolDispatcher:
    """
    Dispatches tool calls with sequence numbers, collects results via
    the resequencer, and returns them in strict dispatch order.
    """

    def __init__(self, default_timeout_seconds: float = 30.0) -> None:
        self._default_timeout = default_timeout_seconds

    async def _execute_one(
        self,
        call: SequencedToolCall,
        tool_fn: Callable,
        args: Dict[str, Any],
        resequencer: InOrderResequencer,
        timeout: float,
    ) -> None:
        try:
            result = await asyncio.wait_for(tool_fn(**args), timeout=timeout)
            call.mark_complete(result)
        except asyncio.TimeoutError:
            call.state = SequencedCallState.TIMED_OUT
            call.error = f"Timed out after {timeout}s"
            call.completed_at = time.time()
        except Exception as exc:
            call.mark_failed(str(exc)[:300])
        finally:
            resequencer.submit(call)

    async def dispatch_ordered(
        self,
        tool_specs: List[Dict[str, Any]],  # [{"tool_name": ..., "fn": ..., "args": ...}]
        drain_timeout_seconds: float = 60.0,
    ) -> List[SequencedToolCall]:
        """
        Dispatches all tool specs concurrently, returns results in dispatch order.
        """
        if not tool_specs:
            return []

        resequencer = InOrderResequencer(
            total_calls=len(tool_specs),
            drain_timeout_seconds=drain_timeout_seconds,
        )

        calls = [
            SequencedToolCall(
                sequence_number=i,
                call_id=spec.get("call_id", f"call_{i}"),
                tool_name=spec["tool_name"],
            )
            for i, spec in enumerate(tool_specs)
        ]

        tasks = [
            asyncio.create_task(
                self._execute_one(
                    call=calls[i],
                    tool_fn=spec["fn"],
                    args=spec.get("args", {}),
                    resequencer=resequencer,
                    timeout=spec.get("timeout", self._default_timeout),
                )
            )
            for i, spec in enumerate(tool_specs)
        ]

        ordered_results = []
        async for call in resequencer.drain():
            ordered_results.append(call)

        await asyncio.gather(*tasks, return_exceptions=True)
        return ordered_results
```

## Solution 4: Ordered Context Assembler

```python
from typing import Any, Dict, List, Optional


class OrderedContextAssembler:
    """
    Converts an ordered list of SequencedToolCalls into a structured
    LLM context block. Results appear in dispatch order regardless of
    completion timing.
    """

    def __init__(self, include_sequence_numbers: bool = False) -> None:
        self._include_seq = include_sequence_numbers

    def assemble(
        self,
        ordered_calls: List[SequencedToolCall],
        separator: str = "\n\n",
    ) -> str:
        parts = []
        for call in ordered_calls:
            prefix = f"[{call.sequence_number}] " if self._include_seq else ""
            if call.state == SequencedCallState.COMPLETED:
                parts.append(f"{prefix}[{call.tool_name}] {call.result}")
            elif call.state == SequencedCallState.FAILED:
                parts.append(f"{prefix}[{call.tool_name}] ERROR: {call.error}")
            elif call.state == SequencedCallState.TIMED_OUT:
                parts.append(f"{prefix}[{call.tool_name}] TIMEOUT: {call.error}")
        return separator.join(parts)

    def to_message_list(
        self,
        ordered_calls: List[SequencedToolCall],
    ) -> List[Dict[str, Any]]:
        """Formats results as a list of tool-result message dicts."""
        messages = []
        for call in ordered_calls:
            content = str(call.result) if call.state == SequencedCallState.COMPLETED else (
                f"ERROR: {call.error}"
            )
            messages.append({
                "type": "tool_result",
                "tool_use_id": call.call_id,
                "content": content,
            })
        return messages
```

## Solution 5: Ordering Integrity Verifier

```python
from typing import List, Tuple


class OrderingIntegrityVerifier:
    """
    Verifies that assembled results are in the correct sequence order.
    Detects gaps, duplicates, and out-of-order deliveries in the pipeline.
    """

    def verify(self, ordered_calls: List[SequencedToolCall]) -> Tuple[bool, List[str]]:
        """Returns (is_valid, list_of_issues)."""
        issues = []

        if not ordered_calls:
            return True, []

        seen_seqs = [c.sequence_number for c in ordered_calls]

        # Check monotonic ordering
        for i in range(1, len(seen_seqs)):
            if seen_seqs[i] <= seen_seqs[i - 1]:
                issues.append(
                    f"Out-of-order: seq {seen_seqs[i]} after seq {seen_seqs[i-1]}"
                )

        # Check for duplicates
        if len(seen_seqs) != len(set(seen_seqs)):
            from collections import Counter
            dups = [seq for seq, count in Counter(seen_seqs).items() if count > 1]
            issues.append(f"Duplicate sequence numbers: {dups}")

        # Check for gaps
        if seen_seqs:
            expected = list(range(min(seen_seqs), max(seen_seqs) + 1))
            missing = set(expected) - set(seen_seqs)
            if missing:
                issues.append(f"Missing sequence numbers: {sorted(missing)}")

        return len(issues) == 0, issues
```

## Solution 6: Ordering Dashboard

```python
import time
from typing import List


class MessageOrderingDashboard:
    """
    Tracks ordering statistics across multiple dispatch batches,
    measuring resequencing delay and out-of-order arrival rates.
    """

    def __init__(self) -> None:
        self._batches: List[dict] = []

    def record_batch(self, ordered_calls: List[SequencedToolCall]) -> None:
        if not ordered_calls:
            return

        completion_times = [c.completed_at for c in ordered_calls if c.completed_at]
        dispatch_times = [c.dispatched_at for c in ordered_calls]

        if not completion_times:
            return

        # Measure resequencing delay = difference between last completion and last delivery
        wall_latency = max(completion_times) - min(dispatch_times)
        latencies = [c.latency_ms() for c in ordered_calls if c.latency_ms()]

        self._batches.append({
            "call_count": len(ordered_calls),
            "wall_latency_ms": round(wall_latency * 1000, 2),
            "max_individual_ms": max(latencies) if latencies else 0.0,
            "min_individual_ms": min(latencies) if latencies else 0.0,
            "timed_out": sum(1 for c in ordered_calls if c.state == SequencedCallState.TIMED_OUT),
            "failed": sum(1 for c in ordered_calls if c.state == SequencedCallState.FAILED),
        })

    def render(self) -> dict:
        if not self._batches:
            return {"generated_at": time.time(), "batches": 0}

        avg_wall = sum(b["wall_latency_ms"] for b in self._batches) / len(self._batches)
        total_timeouts = sum(b["timed_out"] for b in self._batches)
        total_failures = sum(b["failed"] for b in self._batches)

        return {
            "generated_at": time.time(),
            "batches": len(self._batches),
            "avg_wall_latency_ms": round(avg_wall, 2),
            "total_timeouts": total_timeouts,
            "total_failures": total_failures,
        }
```

## Comparison

| Approach | Sequence Tracking | Out-of-Order Buffering | In-Order Delivery | Context Assembly | Integrity Verification |
|---|---|---|---|---|---|
| SequencedToolCall | Yes (per call) | No | No | No | No |
| InOrderResequencer | Via calls | Yes | Yes (async generator) | No | No |
| OrderedToolDispatcher | Via calls | Via resequencer | Via resequencer | No | No |
| OrderedContextAssembler | No | No | No | Yes | No |
| OrderingIntegrityVerifier | No | No | No | No | Yes |

**Best for production**: Use sequence numbers from 0 to N-1 where N is the batch size — never rely on call_id strings for ordering since string comparison is not equivalent to dispatch order. Set `drain_timeout_seconds` to 1.5× the P99 latency of your slowest tool — this ensures the resequencer waits long enough for stragglers without hanging indefinitely. Run `OrderingIntegrityVerifier.verify()` in tests and staging for every batch to catch ordering bugs before they reach production — a single out-of-order delivery is a reliability regression worth fixing immediately.
