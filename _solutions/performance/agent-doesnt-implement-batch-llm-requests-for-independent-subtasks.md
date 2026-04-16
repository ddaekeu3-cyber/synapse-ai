---
title: "Agent Doesn't Implement Batch LLM Requests for Independent Subtasks"
description: "Agents that process independent subtasks sequentially — classifying 50 documents one at a time, evaluating 20 candidates one by one — spend most of their time waiting for serial LLM round trips. Implement batch LLM request execution that identifies independent subtasks, dispatches them concurrently with a configurable concurrency limit, and assembles results in original order."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-batch-llm-requests-for-independent-subtasks
tags: [batch-requests, parallel-llm, concurrency, throughput, independent-subtasks, async-gather]
symptoms:
  - "Classifying 100 items takes 100× a single classification latency — purely serial"
  - "No concurrency limit — either fully serial or unbounded parallel that overwhelms the API"
  - "Results are returned in completion order rather than original input order"
  - "No retry for individual subtask failures within the batch"
  - "Batch throughput is never measured — no comparison to serial baseline"
---

## Why This Happens

LLM APIs are stateless and support concurrent requests. Independent subtasks — classification, extraction, scoring, translation — have no dependencies between them and can be processed in parallel. Serial processing is the default because it is simpler to implement: one call, wait, next call. Parallel processing requires managing concurrency limits (to avoid 429 rate limit errors), collecting results in order, and handling partial failures without aborting the entire batch.

## Solution 1: Subtask Model

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class SubtaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    RETRYING = "retrying"


@dataclass
class LLMSubtask:
    task_id: str
    index: int                    # original position in the batch
    prompt: str
    context: Dict[str, Any] = field(default_factory=dict)
    max_tokens: int = 512
    status: SubtaskStatus = SubtaskStatus.PENDING
    result: Optional[str] = None
    error: Optional[str] = None
    attempt_count: int = 0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None

    @property
    def latency_ms(self) -> Optional[float]:
        if self.started_at and self.completed_at:
            return round((self.completed_at - self.started_at) * 1000, 2)
        return None
```

## Solution 2: Concurrency-Limited Batch Executor

```python
import asyncio
import time
from typing import Any, Callable, List, Optional


class ConcurrencyLimitedBatchExecutor:
    """
    Executes a list of LLMSubtask objects in parallel up to max_concurrent.
    Retries failed tasks up to max_retries times with exponential backoff.
    Returns results in original input order.
    """

    def __init__(
        self,
        max_concurrent: int = 10,
        max_retries: int = 2,
        retry_base_delay_seconds: float = 1.0,
    ):
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_retries = max_retries
        self._retry_delay = retry_base_delay_seconds
        self._total_tasks = 0
        self._failed_tasks = 0
        self._total_latency_ms: float = 0.0

    async def execute_batch(
        self,
        tasks: List[LLMSubtask],
        llm_fn: Callable[[str, int], str],
    ) -> List[LLMSubtask]:
        self._total_tasks += len(tasks)
        coroutines = [self._execute_one(task, llm_fn) for task in tasks]
        completed = await asyncio.gather(*coroutines, return_exceptions=False)
        return list(completed)

    async def _execute_one(
        self,
        task: LLMSubtask,
        llm_fn: Callable,
    ) -> LLMSubtask:
        async with self._semaphore:
            for attempt in range(self._max_retries + 1):
                task.attempt_count = attempt + 1
                task.status = SubtaskStatus.RUNNING
                task.started_at = time.time()
                try:
                    result = await llm_fn(task.prompt, task.max_tokens)
                    task.result = result
                    task.status = SubtaskStatus.DONE
                    task.completed_at = time.time()
                    if task.latency_ms:
                        self._total_latency_ms += task.latency_ms
                    return task
                except Exception as exc:
                    task.error = str(exc)
                    if attempt < self._max_retries:
                        task.status = SubtaskStatus.RETRYING
                        delay = self._retry_delay * (2 ** attempt)
                        await asyncio.sleep(delay)
                    else:
                        task.status = SubtaskStatus.FAILED
                        task.completed_at = time.time()
                        self._failed_tasks += 1
            return task

    def stats(self) -> dict:
        return {
            "total_tasks": self._total_tasks,
            "failed_tasks": self._failed_tasks,
            "failure_rate": round(self._failed_tasks / max(self._total_tasks, 1), 4),
            "avg_latency_ms": round(
                self._total_latency_ms / max(self._total_tasks - self._failed_tasks, 1), 2
            ),
        }
```

## Solution 3: Batch Task Builder

```python
import secrets
from typing import Any, Callable, Dict, List, Optional


class BatchTaskBuilder:
    """
    Constructs LLMSubtask lists from raw inputs using a prompt template.
    Supports per-item context injection and custom max_tokens.
    """

    def __init__(
        self,
        prompt_template: str,
        max_tokens: int = 512,
    ):
        self._template = prompt_template
        self._max_tokens = max_tokens

    def build(
        self,
        items: List[Any],
        context_fn: Optional[Callable[[Any], Dict[str, Any]]] = None,
        item_to_str: Optional[Callable[[Any], str]] = None,
    ) -> List[LLMSubtask]:
        tasks = []
        stringify = item_to_str or str
        for i, item in enumerate(items):
            item_str = stringify(item)
            prompt = self._template.format(item=item_str)
            ctx = context_fn(item) if context_fn else {}
            tasks.append(LLMSubtask(
                task_id=secrets.token_hex(6),
                index=i,
                prompt=prompt,
                context=ctx,
                max_tokens=self._max_tokens,
            ))
        return tasks
```

## Solution 4: Batch Result Assembler

```python
from typing import Any, Callable, List, Optional


class BatchResultAssembler:
    """
    Assembles completed subtask results back into the original input order.
    Handles failed tasks with a configurable fallback value or raises
    if any task failed and strict mode is enabled.
    """

    def __init__(
        self,
        failed_fallback: Any = None,
        strict: bool = False,
    ):
        self._fallback = failed_fallback
        self._strict = strict

    def assemble(self, tasks: List[LLMSubtask]) -> List[Any]:
        ordered = sorted(tasks, key=lambda t: t.index)
        results = []
        for task in ordered:
            if task.status == SubtaskStatus.DONE:
                results.append(task.result)
            elif self._strict:
                raise BatchTaskFailedError(task)
            else:
                results.append(self._fallback)
        return results

    def assemble_with_status(self, tasks: List[LLMSubtask]) -> List[dict]:
        ordered = sorted(tasks, key=lambda t: t.index)
        return [
            {
                "index": t.index,
                "task_id": t.task_id,
                "status": t.status.value,
                "result": t.result,
                "error": t.error,
                "latency_ms": t.latency_ms,
                "attempts": t.attempt_count,
            }
            for t in ordered
        ]


class BatchTaskFailedError(Exception):
    def __init__(self, task: LLMSubtask):
        super().__init__(f"batch task {task.task_id} (index {task.index}) failed: {task.error}")
        self.task = task
```

## Solution 5: Batch Throughput Benchmarker

```python
import asyncio
import time
from typing import Callable, List


class BatchThroughputBenchmarker:
    """
    Measures actual vs theoretical serial throughput for batch LLM execution.
    Helps tune max_concurrent by comparing wall time at different concurrency levels.
    """

    def __init__(self, executor: ConcurrencyLimitedBatchExecutor):
        self._executor = executor

    async def benchmark(
        self,
        tasks: List[LLMSubtask],
        llm_fn: Callable,
    ) -> dict:
        start = time.time()
        completed = await self._executor.execute_batch(tasks, llm_fn)
        wall_time = time.time() - start

        done = [t for t in completed if t.status == SubtaskStatus.DONE]
        failed = [t for t in completed if t.status == SubtaskStatus.FAILED]
        latencies = [t.latency_ms for t in done if t.latency_ms]

        serial_estimate = sum(latencies) / 1000.0 if latencies else 0.0
        speedup = serial_estimate / wall_time if wall_time > 0 else 0.0

        return {
            "task_count": len(tasks),
            "wall_time_seconds": round(wall_time, 2),
            "serial_estimate_seconds": round(serial_estimate, 2),
            "speedup_factor": round(speedup, 2),
            "throughput_tasks_per_second": round(len(done) / wall_time, 2),
            "success_count": len(done),
            "failure_count": len(failed),
        }
```

## Solution 6: Batch LLM Execution Dashboard

```python
import time


class BatchLLMExecutionDashboard:
    """
    Combines executor stats and benchmarker results for operational visibility.
    """

    def __init__(self, executor: ConcurrencyLimitedBatchExecutor):
        self._executor = executor
        self._benchmark_results: list = []

    def record_benchmark(self, result: dict) -> None:
        result["recorded_at"] = time.time()
        self._benchmark_results.append(result)

    def render(self) -> dict:
        latest = self._benchmark_results[-1] if self._benchmark_results else None
        return {
            "generated_at": time.time(),
            "executor_stats": self._executor.stats(),
            "latest_benchmark": latest,
            "benchmark_count": len(self._benchmark_results),
        }
```

## Comparison

| Approach | Parallel Execution | Concurrency Limit | Ordered Results | Per-Task Retry | Throughput Measurement |
|---|---|---|---|---|---|
| ConcurrencyLimitedBatchExecutor | Yes | Yes (semaphore) | Yes (via index) | Yes (backoff) | Via stats() |
| BatchTaskBuilder | No | No | Yes (index assigned) | No | No |
| BatchResultAssembler | No | No | Yes (sort by index) | No | No |
| BatchThroughputBenchmarker | Via executor | Via executor | No | Via executor | Yes |
| BatchLLMExecutionDashboard | No | No | No | No | Yes (aggregate) |

**Best for production**: Start with `max_concurrent=5` and tune upward while monitoring 429 rate-limit errors — the optimal concurrency is the point just below where the API starts throttling. Always assign `index` at construction time so results can be sorted back into input order; callers often depend on positional correspondence between inputs and outputs. Use `max_retries=2` with exponential backoff for transient errors (429, 503) — most API throttling is resolved within 2–4 seconds. Monitor `speedup_factor` from `BatchThroughputBenchmarker`: a speedup below 3× at `max_concurrent=10` suggests the bottleneck has shifted from API latency to token generation rate, and further concurrency increases will not help.
