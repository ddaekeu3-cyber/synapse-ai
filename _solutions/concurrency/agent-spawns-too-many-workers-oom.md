---
layout: solution
title: "Agent Spawns Too Many Workers and Runs Out of Memory — OOM Kill"
category: concurrency
description: "Agent creates one worker per task. With 10,000 tasks, it spawns 10,000 coroutines or threads. Memory exhausted, process OOM killed, all work lost."
tags: [oom, workers, concurrency, semaphore, threadpool, memory, spawn]
---

# Agent Spawns Too Many Workers and Runs Out of Memory — OOM Kill

## Symptom
- Process killed with `Killed` or `MemoryError` under heavy load
- `asyncio.gather(*[process(t) for t in tasks])` with 10,000 tasks causes OOM
- Memory grows linearly with number of tasks before any complete
- `ThreadPoolExecutor` or `ProcessPoolExecutor` spawns thousands of threads
- System swap usage spikes, then OOM killer terminates the process

## Root Cause
Unbounded concurrency: creating one coroutine/thread/process per task with no limit. Even lightweight asyncio coroutines use memory (typically 1–10KB each). 10,000 coroutines = 10–100MB just for the coroutines. Add task data and you exhaust available memory before any work completes.

## Fix

### Option 1: Semaphore to cap concurrent asyncio tasks

```python
import asyncio

async def process_all_tasks(tasks: list, max_concurrent: int = 50) -> list:
    """Process tasks with bounded concurrency"""
    semaphore = asyncio.Semaphore(max_concurrent)

    async def process_one(task):
        async with semaphore:  # Only max_concurrent run at once
            return await process_task(task)

    # Creates coroutines for all tasks but only runs max_concurrent at a time
    return await asyncio.gather(*[process_one(t) for t in tasks])

# Usage
results = await process_all_tasks(ten_thousand_tasks, max_concurrent=20)
```

### Option 2: asyncio.Queue as a worker pool

```python
import asyncio

async def worker_pool(tasks: list, num_workers: int = 10) -> list:
    """Fixed-size worker pool — never exceeds num_workers concurrent tasks"""
    queue = asyncio.Queue()
    results = {}

    # Fill queue
    for i, task in enumerate(tasks):
        await queue.put((i, task))

    async def worker():
        while True:
            try:
                idx, task = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            try:
                results[idx] = await process_task(task)
            except Exception as e:
                results[idx] = {"error": str(e)}
            finally:
                queue.task_done()

    # Start exactly num_workers workers — no more spawned
    workers = [asyncio.create_task(worker()) for _ in range(num_workers)]
    await asyncio.gather(*workers)

    return [results[i] for i in range(len(tasks))]
```

### Option 3: Process in batches to limit memory

```python
import asyncio
from typing import AsyncIterator

async def process_in_batches(
    tasks: list,
    batch_size: int = 100,
    max_concurrent: int = 10
) -> list:
    """Process tasks in batches — only batch_size tasks in memory at once"""
    semaphore = asyncio.Semaphore(max_concurrent)
    all_results = []

    for i in range(0, len(tasks), batch_size):
        batch = tasks[i:i + batch_size]
        print(f"Processing batch {i//batch_size + 1}: tasks {i}–{i+len(batch)}")

        async def process_one(task):
            async with semaphore:
                return await process_task(task)

        batch_results = await asyncio.gather(*[process_one(t) for t in batch])
        all_results.extend(batch_results)

        # Optional: small pause between batches to let GC run
        await asyncio.sleep(0.1)

    return all_results
```

### Option 4: ThreadPoolExecutor with bounded size

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

def process_all_sync(tasks: list, max_workers: int = 10) -> list:
    """Thread pool with hard cap — never spawns more than max_workers threads"""
    results = [None] * len(tasks)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        # Submit all tasks — executor queues excess work
        future_to_idx = {
            executor.submit(process_task_sync, task): i
            for i, task in enumerate(tasks)
        }

        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = {"error": str(e)}
                print(f"Task {idx} failed: {e}")

    return results
```

### Option 5: Monitor memory and back off

```python
import asyncio, psutil, os

async def memory_aware_processing(tasks: list, max_memory_pct: float = 80.0) -> list:
    """Slow down when memory usage is high"""
    semaphore = asyncio.Semaphore(20)
    results = []

    async def process_one(task):
        # Check memory before each task
        mem = psutil.virtual_memory()
        if mem.percent > max_memory_pct:
            print(f"Memory at {mem.percent:.0f}% — pausing for GC")
            await asyncio.sleep(2)

        async with semaphore:
            return await process_task(task)

    return await asyncio.gather(*[process_one(t) for t in tasks])
```

### Option 6: Generator-based processing for huge task sets

```python
import asyncio
from typing import AsyncGenerator

async def task_generator(all_tasks: list) -> AsyncGenerator:
    """Yield tasks one at a time — entire list never in memory"""
    for task in all_tasks:
        yield task

async def process_streaming(task_gen, max_concurrent: int = 20) -> list:
    """Process from generator — O(1) memory for task queue"""
    semaphore = asyncio.Semaphore(max_concurrent)
    results = []

    async def process_one(task):
        async with semaphore:
            return await process_task(task)

    pending = set()
    async for task in task_gen:
        # Limit pending tasks
        while len(pending) >= max_concurrent * 2:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            results.extend(f.result() for f in done)

        pending.add(asyncio.create_task(process_one(task)))

    if pending:
        done, _ = await asyncio.wait(pending)
        results.extend(f.result() for f in done)

    return results
```

## Concurrency Sizing Guide

| Task type | Recommended max_concurrent |
|---|---|
| HTTP API calls (external) | 10–50 |
| Anthropic API calls | Rate limit ÷ avg response time |
| Database queries | 5–20 (match connection pool size) |
| File I/O | 20–100 |
| CPU-bound tasks | `os.cpu_count()` |
| Mixed I/O + CPU | 20–50 |

## Memory per Worker Type

| Worker type | Memory per worker |
|---|---|
| asyncio coroutine | 1–10KB |
| Thread | 1–8MB (stack) |
| Process | 20–100MB+ |
| Docker container | 50MB–2GB+ |

## Expected Token Savings
OOM kill + restart + re-processing all work: ~30,000 tokens
Semaphore cap prevents it: 0 wasted

## Environment
- High-volume agents processing large task batches
- Source: direct experience with production OOM incidents in agent pipelines
