---
layout: solution
title: "Agent Silently Drops Failed Subtasks — Missing Results Without Error"
category: general
description: "Agent runs 10 parallel subtasks. Two fail silently. Agent collects the 8 successful results and proceeds as if all 10 completed. The final report is missing data from the 2 failed subtasks. No error is raised. No warning is logged."
tags: [general, subtask, silent-failure, error-handling, asyncio, gather, partial-results, validation]
---

# Agent Silently Drops Failed Subtasks — Missing Results Without Error

## Symptom
- Report covers 80 out of 100 expected items — 20 are silently missing
- `asyncio.gather(*tasks, return_exceptions=True)` catches exceptions but they are never inspected
- Agent says "processed all items" but some were skipped due to timeout
- Downstream system receives partial data — no indication that data is incomplete
- Failed subtasks logged at DEBUG level — not visible in production log level
- Agent result count is wrong but no assertion checks it

## Root Cause
`asyncio.gather(..., return_exceptions=True)` returns exceptions as result values instead of raising them. If the code that processes results doesn't check for `isinstance(result, Exception)`, exceptions are silently discarded. Similarly, try/except blocks inside subtasks that catch and swallow exceptions mean the caller never knows the subtask failed. Missing results look the same as empty results unless the expected count is verified against the actual count.

## Fix

### Option 1: Inspect all results — treat exceptions as failures

```python
import asyncio
from dataclasses import dataclass
from typing import TypeVar, Generic

T = TypeVar("T")

@dataclass
class SubtaskResult(Generic[T]):
    task_id: str
    success: bool
    result: T | None
    error: Exception | None = None

    @classmethod
    def ok(cls, task_id: str, result: T) -> "SubtaskResult[T]":
        return cls(task_id=task_id, success=True, result=result)

    @classmethod
    def failed(cls, task_id: str, error: Exception) -> "SubtaskResult[T]":
        return cls(task_id=task_id, success=False, result=None, error=error)

async def run_subtasks_with_accounting(
    tasks: dict[str, asyncio.coroutine],
    fail_on_any_error: bool = False,
    min_success_rate: float = 1.0
) -> tuple[list[SubtaskResult], dict]:
    """
    Run subtasks and account for every result — successes AND failures.
    Returns all results plus a summary report.

    fail_on_any_error: if True, raises if any subtask fails
    min_success_rate: minimum fraction of successes (0.8 = 80% must succeed)
    """
    task_ids = list(tasks.keys())
    coros = list(tasks.values())

    # return_exceptions=True — we want to inspect, not crash
    raw_results = await asyncio.gather(*coros, return_exceptions=True)

    results = []
    for task_id, raw in zip(task_ids, raw_results):
        if isinstance(raw, Exception):
            results.append(SubtaskResult.failed(task_id, raw))
            print(f"Subtask '{task_id}' FAILED: {type(raw).__name__}: {raw}")
        else:
            results.append(SubtaskResult.ok(task_id, raw))

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]
    success_rate = len(successes) / len(results) if results else 0

    summary = {
        "total": len(results),
        "succeeded": len(successes),
        "failed": len(failures),
        "success_rate": success_rate,
        "failed_tasks": [{"id": r.task_id, "error": str(r.error)} for r in failures]
    }

    print(
        f"Subtasks complete: {len(successes)}/{len(results)} succeeded "
        f"({success_rate*100:.0f}%)"
    )

    if fail_on_any_error and failures:
        raise RuntimeError(
            f"{len(failures)} subtask(s) failed: "
            + ", ".join(f"{r.task_id}: {r.error}" for r in failures)
        )

    if success_rate < min_success_rate:
        raise RuntimeError(
            f"Success rate {success_rate*100:.0f}% below minimum {min_success_rate*100:.0f}%. "
            f"Failed: {[r.task_id for r in failures]}"
        )

    return results, summary

# Usage:
tasks = {
    f"process_item_{i}": process_item(item)
    for i, item in enumerate(items)
}

results, summary = await run_subtasks_with_accounting(
    tasks,
    min_success_rate=0.95  # At least 95% must succeed
)

# Use only successful results — don't pretend failures didn't happen:
successful_data = [r.result for r in results if r.success]

if summary["failed"]:
    print(f"WARNING: {summary['failed']} items were not processed: {summary['failed_tasks']}")
```

### Option 2: Result collector with expected count validation

```python
from dataclasses import dataclass, field

@dataclass
class BatchResult:
    """Tracks expected vs actual results — fails if count doesn't match"""
    expected_count: int
    _results: list = field(default_factory=list)
    _errors: list[dict] = field(default_factory=list)

    def add_success(self, item_id: str, data: any):
        self._results.append({"id": item_id, "data": data})

    def add_failure(self, item_id: str, error: Exception):
        self._errors.append({"id": item_id, "error": str(error), "type": type(error).__name__})
        print(f"Failed: {item_id} — {error}")

    def validate(self, strict: bool = True) -> "BatchResult":
        """
        Validate that all expected items were processed.
        strict=True: raise if any failures
        strict=False: raise only if NO successes
        """
        total_processed = len(self._results) + len(self._errors)

        if total_processed != self.expected_count:
            raise RuntimeError(
                f"Expected {self.expected_count} results, got {total_processed}. "
                f"Some subtasks may have been silently skipped."
            )

        if strict and self._errors:
            raise RuntimeError(
                f"{len(self._errors)}/{self.expected_count} items failed:\n"
                + "\n".join(f"  {e['id']}: {e['error']}" for e in self._errors)
            )

        if not self._results:
            raise RuntimeError("All subtasks failed — no successful results")

        return self

    @property
    def results(self) -> list:
        return [r["data"] for r in self._results]

    @property
    def completeness(self) -> float:
        return len(self._results) / self.expected_count if self.expected_count > 0 else 0

async def process_batch(items: list[dict]) -> BatchResult:
    batch = BatchResult(expected_count=len(items))

    for item in items:
        try:
            result = await process_item(item)
            batch.add_success(item["id"], result)
        except Exception as e:
            batch.add_failure(item["id"], e)

    return batch.validate(strict=False)  # Warn on partial, fail on total

batch = await process_batch(all_items)
print(f"Completeness: {batch.completeness*100:.0f}%")

if batch.completeness < 1.0:
    # Partial results — notify downstream that data is incomplete
    incomplete_notice = f"WARNING: {batch.expected_count - len(batch.results)} items missing from results"
```

### Option 3: Structured error propagation — never swallow exceptions silently

```python
import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

@asynccontextmanager
async def subtask_context(task_name: str, task_id: str, result_collector: list):
    """
    Context manager for subtasks — always records outcome (success or failure).
    Never swallows exceptions silently.
    """
    try:
        yield
        # If we reach here, the subtask succeeded (the caller adds to result_collector)
    except asyncio.CancelledError:
        logger.warning(f"Subtask '{task_name}:{task_id}' was cancelled")
        result_collector.append({
            "id": task_id,
            "status": "cancelled",
            "error": "Task was cancelled"
        })
    except Exception as e:
        logger.error(
            f"Subtask '{task_name}:{task_id}' failed",
            exc_info=True,
            extra={"task_name": task_name, "task_id": task_id, "error": str(e)}
        )
        result_collector.append({
            "id": task_id,
            "status": "error",
            "error": str(e),
            "error_type": type(e).__name__
        })

async def process_item_safely(item: dict, all_results: list) -> None:
    """Wrapper that always records outcome — never silently drops"""
    async with subtask_context("process_item", item["id"], all_results):
        data = await process_item(item)
        all_results.append({"id": item["id"], "status": "success", "data": data})

async def process_all_items(items: list[dict]) -> list[dict]:
    all_results = []

    await asyncio.gather(
        *[process_item_safely(item, all_results) for item in items],
        return_exceptions=False  # Exceptions are handled inside — this won't raise
    )

    # Verify completeness
    assert len(all_results) == len(items), (
        f"Result count mismatch: expected {len(items)}, got {len(all_results)}"
    )

    errors = [r for r in all_results if r["status"] != "success"]
    if errors:
        logger.warning(
            f"Batch complete with {len(errors)} errors",
            extra={"errors": errors, "success_count": len(items) - len(errors)}
        )

    return all_results
```

### Option 4: Timeout guard for each subtask

```python
import asyncio

async def bounded_subtask(
    coro,
    task_id: str,
    timeout: float = 30.0
) -> SubtaskResult:
    """
    Run a subtask with timeout — explicitly fail instead of hanging silently.
    """
    try:
        result = await asyncio.wait_for(coro, timeout=timeout)
        return SubtaskResult.ok(task_id, result)
    except asyncio.TimeoutError:
        return SubtaskResult.failed(
            task_id,
            TimeoutError(f"Subtask '{task_id}' timed out after {timeout}s")
        )
    except Exception as e:
        return SubtaskResult.failed(task_id, e)

async def run_all_subtasks_bounded(
    item_tasks: list[tuple[str, asyncio.coroutine]],
    per_task_timeout: float = 30.0,
    max_concurrent: int = 10
) -> list[SubtaskResult]:
    """
    Run all subtasks with per-task timeouts and concurrency limits.
    Every subtask produces exactly one result — no silent drops.
    """
    semaphore = asyncio.Semaphore(max_concurrent)

    async def run_one(task_id: str, coro) -> SubtaskResult:
        async with semaphore:
            return await bounded_subtask(coro, task_id, per_task_timeout)

    results = await asyncio.gather(
        *[run_one(tid, coro) for tid, coro in item_tasks]
    )

    # Guaranteed: len(results) == len(item_tasks)
    assert len(results) == len(item_tasks), "Internal error: result count mismatch"

    return list(results)
```

### Option 5: Partial result warnings in agent response

```python
import anthropic

def build_partial_result_context(
    summary: dict,
    partial_results: list
) -> str:
    """
    Build context that makes partial results explicit to the model.
    Prevents agent from presenting incomplete data as complete.
    """
    if summary["failed"] == 0:
        return f"All {summary['total']} items processed successfully."

    failed_pct = summary['failed'] / summary['total'] * 100

    context = f"""DATA COMPLETENESS WARNING:
- Processed: {summary['succeeded']}/{summary['total']} items ({100-failed_pct:.0f}% complete)
- Failed: {summary['failed']} items ({failed_pct:.0f}% of data is MISSING)
- Failed item IDs: {[f['id'] for f in summary['failed_tasks'][:10]]}

IMPORTANT: Your response must acknowledge that {summary['failed']} items are missing.
Do not present results as complete. Note which items are unavailable.

Available data ({summary['succeeded']} items):
{partial_results}"""

    return context

client = anthropic.Anthropic()

async def generate_report_with_completeness_check(
    items: list[dict],
    results: list[SubtaskResult],
    summary: dict
) -> str:
    successful_data = [r.result for r in results if r.success]

    system = """You are a data analyst. When data is incomplete,
ALWAYS state clearly what is missing and how many items were affected.
Never present partial results as if they cover all items."""

    context = build_partial_result_context(summary, successful_data)

    response = client.messages.create(
        model="claude-sonnet-4-6",
        system=system,
        messages=[{
            "role": "user",
            "content": f"Generate a summary report:\n\n{context}"
        }],
        max_tokens=2048
    )

    return response.content[0].text
```

### Option 6: Retry failed subtasks before finalizing

```python
async def run_with_retry(
    item_tasks: dict[str, asyncio.coroutine],
    max_retries: int = 2,
    retry_delay: float = 5.0
) -> list[SubtaskResult]:
    """
    Run subtasks, retry failed ones, report final state.
    """
    all_results: dict[str, SubtaskResult] = {}

    # Initial run
    initial = await asyncio.gather(
        *[asyncio.create_task(coro, name=tid) for tid, coro in item_tasks.items()],
        return_exceptions=True
    )

    for task_id, result in zip(item_tasks.keys(), initial):
        if isinstance(result, Exception):
            all_results[task_id] = SubtaskResult.failed(task_id, result)
        else:
            all_results[task_id] = SubtaskResult.ok(task_id, result)

    # Retry failed tasks
    for attempt in range(max_retries):
        failed_ids = [tid for tid, r in all_results.items() if not r.success]
        if not failed_ids:
            break

        print(f"Retrying {len(failed_ids)} failed tasks (attempt {attempt + 2}/{max_retries + 1})")
        await asyncio.sleep(retry_delay * (attempt + 1))

        retry_results = await asyncio.gather(
            *[item_tasks[tid] for tid in failed_ids],
            return_exceptions=True
        )

        for task_id, result in zip(failed_ids, retry_results):
            if isinstance(result, Exception):
                all_results[task_id] = SubtaskResult.failed(task_id, result)
                print(f"Retry failed: {task_id} — {result}")
            else:
                all_results[task_id] = SubtaskResult.ok(task_id, result)
                print(f"Retry succeeded: {task_id}")

    final_results = list(all_results.values())
    still_failed = [r for r in final_results if not r.success]

    if still_failed:
        print(
            f"Final: {len(final_results) - len(still_failed)}/{len(final_results)} succeeded. "
            f"Permanent failures: {[r.task_id for r in still_failed]}"
        )

    return final_results
```

## Failure Detection Checklist

| Check | Code Pattern | Catches |
|---|---|---|
| Inspect gather results | `isinstance(r, Exception)` | All asyncio exceptions |
| Validate result count | `assert len(results) == expected` | Silent drops |
| Minimum success rate | `success_rate >= threshold` | Partial batch failures |
| Per-task timeout | `asyncio.wait_for(coro, timeout)` | Hanging subtasks |
| Log failures at WARNING+ | `logger.warning(...)` | Visibility in production |
| Partial result notice | Tell model data is incomplete | Hallucinated completeness |

## Expected Token Savings
Agent presents partial results as complete → downstream acts on wrong data → debug session: ~20,000 tokens
Explicit failure accounting → partial results flagged → corrective action taken immediately: 0 hidden failures

## Environment
- Any agent running parallel subtasks, batch processing, or multi-source data collection; critical for reporting and aggregation agents where missing data is not obvious in the output
- Source: direct experience; silently dropped subtasks are the hardest class of agent bug to detect because the output looks plausible but is factually incomplete
