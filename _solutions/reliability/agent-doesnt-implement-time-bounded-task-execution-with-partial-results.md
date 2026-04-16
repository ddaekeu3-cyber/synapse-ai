---
title: "Agent Doesn't Implement Time-Bounded Task Execution with Partial Results"
description: "Six solutions for returning best-effort partial results when an agent task approaches its deadline, rather than timing out with nothing."
difficulty: intermediate
category: reliability
tags: [timeout, partial-results, deadline, graceful-degradation, reliability, sla]
---

# Agent Doesn't Implement Time-Bounded Task Execution with Partial Results

When an agent hits a deadline it usually returns an error—even if 90% of the work is done. Users get nothing instead of something useful. Time-bounded execution with partial result return lets agents deliver what they've completed so far, clearly marked as partial, whenever the clock runs out.

## Solution 1: Deadline-Propagating Context with Partial Accumulator

Pass a deadline context through all subtasks; accumulate results as they complete; return whatever is ready at cutoff.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any
from anthropic import AsyncAnthropic


@dataclass
class DeadlineContext:
    deadline: float  # Unix timestamp

    @classmethod
    def from_timeout(cls, timeout_seconds: float) -> "DeadlineContext":
        return cls(deadline=time.time() + timeout_seconds)

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - time.time())

    @property
    def expired(self) -> bool:
        return time.time() >= self.deadline

    def child(self, max_fraction: float = 0.8) -> "DeadlineContext":
        """Create a child context with a tighter deadline."""
        remaining = self.remaining
        return DeadlineContext(deadline=time.time() + remaining * max_fraction)


@dataclass
class PartialResult:
    completed: list[Any] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    is_partial: bool = False
    completed_at: float = field(default_factory=time.time)

    def add(self, item: Any):
        self.completed.append(item)

    def mark_pending(self, label: str):
        self.pending.append(label)
        self.is_partial = True


class DeadlineAgent:
    def __init__(self):
        self.client = AsyncAnthropic()

    async def _bounded_llm_call(
        self, ctx: DeadlineContext, prompt: str, max_tokens: int = 512
    ) -> str | None:
        """LLM call that respects deadline; returns None if deadline exceeded."""
        if ctx.expired:
            return None
        try:
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=max_tokens,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=ctx.remaining,
            )
            return response.content[0].text
        except asyncio.TimeoutError:
            return None

    async def process_items(
        self,
        items: list[str],
        ctx: DeadlineContext,
        processor_prompt: str = "Summarize this item in one sentence: {item}",
    ) -> PartialResult:
        result = PartialResult()
        for item in items:
            if ctx.expired:
                result.mark_pending(item)
                continue
            child_ctx = ctx.child(max_fraction=0.9)
            output = await self._bounded_llm_call(
                child_ctx,
                processor_prompt.format(item=item),
            )
            if output is None:
                result.mark_pending(item)
                result.is_partial = True
            else:
                result.add({"item": item, "summary": output})

        return result

    async def analyze_with_deadline(
        self,
        topics: list[str],
        timeout_seconds: float = 10.0,
    ) -> PartialResult:
        ctx = DeadlineContext.from_timeout(timeout_seconds)
        result = await self.process_items(topics, ctx)
        if result.is_partial:
            print(
                f"[PARTIAL] Completed {len(result.completed)}/{len(topics)} items. "
                f"Pending: {result.pending}"
            )
        return result


async def demo_deadline_context():
    agent = DeadlineAgent()
    topics = [
        "quantum computing", "machine learning", "blockchain",
        "edge computing", "federated learning", "neuromorphic chips",
    ]
    result = await agent.analyze_with_deadline(topics, timeout_seconds=8.0)
    print(f"Completed: {len(result.completed)}, Partial: {result.is_partial}")
    for item in result.completed:
        print(f"  {item['item']}: {item['summary'][:60]}")
    if result.pending:
        print(f"  Pending (not processed): {result.pending}")
```

## Solution 2: Streaming Partial Results via AsyncGenerator

Yield each result as it completes; callers receive a live stream and can stop consuming at any time.

```python
import asyncio
import time
from dataclasses import dataclass
from typing import AsyncGenerator
from anthropic import AsyncAnthropic


@dataclass
class StreamedResult:
    index: int
    item: str
    result: str
    elapsed_ms: float
    is_final: bool = False  # True on the last item


class StreamingPartialAgent:
    def __init__(self):
        self.client = AsyncAnthropic()

    async def process_stream(
        self,
        items: list[str],
        timeout_per_item: float = 5.0,
        total_timeout: float = 30.0,
    ) -> AsyncGenerator[StreamedResult, None]:
        """Yield results as they complete; caller decides when to stop."""
        deadline = time.time() + total_timeout

        for i, item in enumerate(items):
            remaining_total = deadline - time.time()
            if remaining_total <= 0:
                print(f"[STREAM] Total deadline exceeded at item {i}")
                break

            per_item_timeout = min(timeout_per_item, remaining_total)
            start = time.perf_counter()
            try:
                response = await asyncio.wait_for(
                    self.client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=256,
                        messages=[{
                            "role": "user",
                            "content": f"Briefly explain: {item}",
                        }],
                    ),
                    timeout=per_item_timeout,
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                yield StreamedResult(
                    index=i,
                    item=item,
                    result=response.content[0].text,
                    elapsed_ms=elapsed_ms,
                    is_final=(i == len(items) - 1),
                )
            except asyncio.TimeoutError:
                elapsed_ms = (time.perf_counter() - start) * 1000
                yield StreamedResult(
                    index=i,
                    item=item,
                    result="[TIMED_OUT]",
                    elapsed_ms=elapsed_ms,
                    is_final=(i == len(items) - 1),
                )


async def demo_streaming_partial():
    agent = StreamingPartialAgent()
    items = ["neural networks", "transformer architecture", "attention mechanism",
             "RLHF", "chain of thought", "retrieval augmented generation"]

    completed = 0
    async for result in agent.process_stream(items, timeout_per_item=4.0, total_timeout=15.0):
        if result.result != "[TIMED_OUT]":
            completed += 1
            print(f"[{result.index}] {result.item}: {result.result[:60]}... ({result.elapsed_ms:.0f}ms)")
        else:
            print(f"[{result.index}] {result.item}: TIMED OUT")
    print(f"\nCompleted {completed}/{len(items)} items")
```

## Solution 3: Budget-Time Task Planner with Early Exit

Plan all subtasks upfront; allocate time budget per task; exit early and return partial plan when budget exhausts.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class PlannedSubtask:
    name: str
    prompt: str
    priority: int = 1       # Higher = more important
    time_budget_s: float = 5.0
    result: str | None = None
    skipped: bool = False
    actual_ms: float = 0.0


@dataclass
class BudgetedPlan:
    subtasks: list[PlannedSubtask] = field(default_factory=list)
    total_budget_s: float = 30.0
    _spent_s: float = 0.0

    @property
    def remaining_budget(self) -> float:
        return max(0.0, self.total_budget_s - self._spent_s)

    def spend(self, seconds: float):
        self._spent_s += seconds

    @property
    def completion_rate(self) -> float:
        done = sum(1 for t in self.subtasks if t.result is not None)
        return done / max(len(self.subtasks), 1)

    def summary(self) -> dict:
        return {
            "total_subtasks": len(self.subtasks),
            "completed": sum(1 for t in self.subtasks if t.result),
            "skipped": sum(1 for t in self.subtasks if t.skipped),
            "completion_rate": round(self.completion_rate, 3),
            "time_spent_s": round(self._spent_s, 2),
            "is_partial": any(t.skipped for t in self.subtasks),
        }


class BudgetedPlanAgent:
    def __init__(self):
        self.client = AsyncAnthropic()

    async def _execute_subtask(
        self, subtask: PlannedSubtask, time_budget: float
    ) -> bool:
        """Returns True if completed within budget."""
        start = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=[{"role": "user", "content": subtask.prompt}],
                ),
                timeout=time_budget,
            )
            subtask.result = response.content[0].text
            subtask.actual_ms = (time.perf_counter() - start) * 1000
            return True
        except asyncio.TimeoutError:
            subtask.actual_ms = (time.perf_counter() - start) * 1000
            return False

    async def run_plan(self, plan: BudgetedPlan) -> BudgetedPlan:
        # Sort by priority (high priority first)
        ordered = sorted(plan.subtasks, key=lambda t: -t.priority)

        for subtask in ordered:
            if plan.remaining_budget <= 0:
                subtask.skipped = True
                continue

            budget = min(subtask.time_budget_s, plan.remaining_budget)
            start = time.time()
            completed = await self._execute_subtask(subtask, budget)
            spent = time.time() - start
            plan.spend(spent)

            if not completed:
                subtask.skipped = True
                print(
                    f"[BUDGET] '{subtask.name}' timed out. "
                    f"Remaining: {plan.remaining_budget:.1f}s"
                )
            else:
                print(f"[BUDGET] '{subtask.name}' done ({spent*1000:.0f}ms)")

        return plan


async def demo_budgeted_plan():
    agent = BudgetedPlanAgent()
    plan = BudgetedPlan(total_budget_s=12.0, subtasks=[
        PlannedSubtask("intro", "Write a one-sentence intro to AI.", priority=3, time_budget_s=4.0),
        PlannedSubtask("history", "Summarize AI history in 2 sentences.", priority=2, time_budget_s=4.0),
        PlannedSubtask("future", "Predict AI trends in 2 sentences.", priority=1, time_budget_s=4.0),
        PlannedSubtask("ethics", "List 3 AI ethics concerns.", priority=2, time_budget_s=4.0),
    ])
    result = await agent.run_plan(plan)
    print("\n=== Plan Summary ===")
    print(result.summary())
    for t in result.subtasks:
        status = "OK" if t.result else ("SKIP" if t.skipped else "?")
        print(f"  [{status}] {t.name}: {(t.result or '')[:60]}")
```

## Solution 4: Cooperative Checkpointing with Resume-on-Timeout

Tasks write checkpoints after each unit of work; on timeout, return the last checkpoint as the partial result.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any
from anthropic import AsyncAnthropic


@dataclass
class Checkpoint:
    step: int
    data: Any
    timestamp: float = field(default_factory=time.time)
    label: str = ""


class CheckpointedTask:
    def __init__(self, task_id: str):
        self.task_id = task_id
        self._checkpoints: list[Checkpoint] = []

    def save(self, step: int, data: Any, label: str = ""):
        cp = Checkpoint(step=step, data=data, label=label)
        self._checkpoints.append(cp)

    @property
    def last_checkpoint(self) -> Checkpoint | None:
        return self._checkpoints[-1] if self._checkpoints else None

    @property
    def checkpoint_count(self) -> int:
        return len(self._checkpoints)

    def partial_result(self) -> dict:
        if not self._checkpoints:
            return {"status": "no_progress", "data": None}
        last = self._checkpoints[-1]
        return {
            "status": "partial",
            "last_step": last.step,
            "label": last.label,
            "data": last.data,
            "checkpoint_count": len(self._checkpoints),
            "last_checkpoint_at": last.timestamp,
        }


class CheckpointingAgent:
    def __init__(self):
        self.client = AsyncAnthropic()

    async def _step(self, prompt: str, timeout: float) -> str | None:
        try:
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout,
            )
            return response.content[0].text
        except asyncio.TimeoutError:
            return None

    async def multi_step_analysis(
        self,
        topic: str,
        total_timeout: float = 15.0,
    ) -> dict:
        task = CheckpointedTask(task_id=topic[:20])
        deadline = time.time() + total_timeout
        steps = [
            ("Define the core concept", 1),
            ("List key components or aspects", 2),
            ("Describe real-world applications", 3),
            ("Identify challenges or limitations", 4),
            ("Summarize future outlook", 5),
        ]

        for label, step_num in steps:
            remaining = deadline - time.time()
            if remaining <= 1.0:
                print(f"[CHECKPOINT] Deadline approaching; returning partial at step {step_num}")
                break

            result = await self._step(
                f"For the topic '{topic}': {label}. Be concise (2-3 sentences).",
                timeout=min(remaining * 0.6, 6.0),
            )
            if result is None:
                print(f"[CHECKPOINT] Step '{label}' timed out; returning partial")
                break

            task.save(step_num, result, label=label)
            print(f"[CHECKPOINT] Step {step_num} '{label}' saved")

        if task.checkpoint_count == len(steps):
            return {
                "status": "complete",
                "steps": [
                    {"label": cp.label, "content": cp.data}
                    for cp in task._checkpoints
                ],
            }
        return task.partial_result()


async def demo_checkpointing():
    agent = CheckpointingAgent()
    result = await agent.multi_step_analysis("large language models", total_timeout=12.0)
    print(f"\nResult status: {result['status']}")
    if result["status"] == "complete":
        for step in result["steps"]:
            print(f"  {step['label']}: {step['content'][:60]}")
    else:
        print(f"  Last step: {result.get('label')}")
        print(f"  Partial data: {str(result.get('data', ''))[:120]}")
```

## Solution 5: Parallel Race with Best-Partial Collector

Run subtasks in parallel; collect all that finish before the deadline; return the set that completed.

```python
import asyncio
import time
from dataclasses import dataclass, field
from anthropic import AsyncAnthropic


@dataclass
class ParallelResult:
    task_id: str
    prompt: str
    result: str | None
    elapsed_ms: float
    timed_out: bool


class ParallelPartialAgent:
    def __init__(self):
        self.client = AsyncAnthropic()

    async def _run_one(self, task_id: str, prompt: str, timeout: float) -> ParallelResult:
        start = time.perf_counter()
        try:
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout,
            )
            elapsed = (time.perf_counter() - start) * 1000
            return ParallelResult(task_id, prompt, response.content[0].text, elapsed, False)
        except asyncio.TimeoutError:
            elapsed = (time.perf_counter() - start) * 1000
            return ParallelResult(task_id, prompt, None, elapsed, True)

    async def run_parallel_with_deadline(
        self,
        tasks: dict[str, str],  # task_id -> prompt
        deadline_seconds: float = 10.0,
    ) -> dict:
        """All tasks run in parallel; return whatever finishes before deadline."""
        coros = [
            self._run_one(tid, prompt, deadline_seconds)
            for tid, prompt in tasks.items()
        ]
        results = await asyncio.gather(*coros)
        completed = [r for r in results if not r.timed_out]
        timed_out = [r for r in results if r.timed_out]

        return {
            "is_partial": len(timed_out) > 0,
            "completed_count": len(completed),
            "timed_out_count": len(timed_out),
            "completion_rate": round(len(completed) / max(len(results), 1), 3),
            "results": {r.task_id: r.result for r in completed},
            "timed_out_tasks": [r.task_id for r in timed_out],
            "avg_latency_ms": round(
                sum(r.elapsed_ms for r in completed) / max(len(completed), 1), 1
            ),
        }


async def demo_parallel_partial():
    agent = ParallelPartialAgent()
    tasks = {
        "python": "What is Python? One sentence.",
        "rust": "What is Rust? One sentence.",
        "go": "What is Go? One sentence.",
        "typescript": "What is TypeScript? One sentence.",
        "haskell": "What is Haskell? One sentence.",
    }
    result = await agent.run_parallel_with_deadline(tasks, deadline_seconds=8.0)
    print(f"Completed: {result['completed_count']}/{len(tasks)}")
    print(f"Is partial: {result['is_partial']}")
    for tid, text in result["results"].items():
        print(f"  {tid}: {text[:70]}")
    if result["timed_out_tasks"]:
        print(f"  Timed out: {result['timed_out_tasks']}")
```

## Solution 6: SLA-Enforced Wrapper with Degraded Response Generation

If the primary task times out, generate a lower-quality degraded response within the remaining budget.

```python
import asyncio
import time
from dataclasses import dataclass
from enum import Enum
from anthropic import AsyncAnthropic


class ResponseQuality(Enum):
    FULL = "full"
    DEGRADED = "degraded"
    MINIMAL = "minimal"
    TIMEOUT = "timeout"


@dataclass
class SLAResponse:
    content: str
    quality: ResponseQuality
    latency_ms: float
    sla_met: bool  # True if responded within SLA window


class SLAEnforcedAgent:
    """
    Three-tier response: full (primary) -> degraded (fallback) -> minimal (last resort).
    Ensures something useful is always returned within the SLA.
    """

    def __init__(
        self,
        full_timeout: float = 8.0,
        degraded_timeout: float = 3.0,
        minimal_timeout: float = 1.5,
        sla_seconds: float = 10.0,
    ):
        self.client = AsyncAnthropic()
        self.full_timeout = full_timeout
        self.degraded_timeout = degraded_timeout
        self.minimal_timeout = minimal_timeout
        self.sla_seconds = sla_seconds

    async def _llm(self, prompt: str, system: str, timeout: float, max_tokens: int) -> str | None:
        try:
            response = await asyncio.wait_for(
                self.client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=timeout,
            )
            return response.content[0].text
        except asyncio.TimeoutError:
            return None

    async def respond(self, message: str) -> SLAResponse:
        sla_deadline = time.time() + self.sla_seconds
        overall_start = time.perf_counter()

        # Tier 1: Full response
        result = await self._llm(
            message,
            system="Provide a thorough, detailed answer.",
            timeout=self.full_timeout,
            max_tokens=1024,
        )
        if result is not None:
            latency = (time.perf_counter() - overall_start) * 1000
            return SLAResponse(result, ResponseQuality.FULL, latency, time.time() <= sla_deadline)

        # Tier 2: Degraded (shorter, simpler)
        remaining = sla_deadline - time.time()
        if remaining > 0:
            result = await self._llm(
                f"Answer briefly in 1-2 sentences: {message}",
                system="Be concise. Summarize only the most important point.",
                timeout=min(self.degraded_timeout, remaining),
                max_tokens=256,
            )
            if result is not None:
                latency = (time.perf_counter() - overall_start) * 1000
                return SLAResponse(
                    f"[Partial answer] {result}",
                    ResponseQuality.DEGRADED,
                    latency,
                    time.time() <= sla_deadline,
                )

        # Tier 3: Minimal — one-word or acknowledgement
        remaining = sla_deadline - time.time()
        if remaining > 0:
            result = await self._llm(
                message,
                system="Reply in exactly one sentence.",
                timeout=min(self.minimal_timeout, remaining),
                max_tokens=64,
            )
            if result is not None:
                latency = (time.perf_counter() - overall_start) * 1000
                return SLAResponse(
                    f"[Minimal answer] {result}",
                    ResponseQuality.MINIMAL,
                    latency,
                    time.time() <= sla_deadline,
                )

        latency = (time.perf_counter() - overall_start) * 1000
        return SLAResponse(
            "I was unable to generate a response within the time limit. Please try again.",
            ResponseQuality.TIMEOUT,
            latency,
            sla_met=False,
        )


async def demo_sla():
    agent = SLAEnforcedAgent(full_timeout=6.0, degraded_timeout=3.0, minimal_timeout=1.5, sla_seconds=10.0)
    questions = [
        "Explain the entire history of computing from 1940 to present.",
        "What is 2+2?",
        "Write a 10,000-word essay on climate change.",
    ]
    for q in questions:
        response = await agent.respond(q)
        print(f"\nQ: {q[:60]}")
        print(f"Quality: {response.quality.value}, SLA met: {response.sla_met}, Latency: {response.latency_ms:.0f}ms")
        print(f"Response: {response.content[:100]}")
```

## Comparison Table

| Solution | Partial Delivery | Deadline Propagation | Checkpointing | Parallel Support | Best For |
|---|---|---|---|---|---|
| Deadline Context | Per-item accumulator | Yes (child ctx) | No | No | Sequential multi-item processing |
| Streaming Generator | Yield-as-ready | Per-item timeout | No | No | Streaming APIs, live UIs |
| Budgeted Plan | Priority-ordered execution | Budget allocation | No | No | Heterogeneous task plans |
| Checkpoint + Resume | Last checkpoint | Remaining time | Yes | No | Long-running stateful tasks |
| Parallel Race | Best-N-of-M | Shared deadline | No | Yes | Independent parallel subtasks |
| SLA-Enforced Tiers | Degraded quality fallback | SLA window | No | No | User-facing SLA-bound agents |

**Recommended**: Use **Deadline Context** (Solution 1) for sequential pipelines where you want maximum results per deadline. Use **Parallel Race** (Solution 5) when subtasks are independent. Use **SLA-Enforced Tiers** (Solution 6) for user-facing agents where *some* response is always better than a timeout error, regardless of quality. Combine **Checkpointing** (Solution 4) with any approach for tasks that can be resumed across sessions.
