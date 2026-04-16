---
title: "Agent Doesn't Implement Partitioned Work Queue for Large Batch Jobs"
description: "Agents processing large batch jobs with a single global queue create hot spots: one worker handles all items for a popular key while others sit idle, and a single slow item blocks the entire queue. Implement partitioned work queues to distribute load evenly across workers by routing items to dedicated key-affinity partitions."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-partitioned-work-queue-for-large-batch-jobs
tags: [partitioned-queue, batch-processing, reliability, load-distribution, work-queue, key-affinity]
symptoms:
  - "One worker processes 80% of all batch items while others are idle"
  - "All emails for tenant A queue behind each other because tenant ID is a hot key"
  - "Single slow batch item blocks all subsequent items in the queue"
  - "Global queue becomes a bottleneck under high batch concurrency"
  - "Reprocessing a 1M-item batch reruns everything from scratch on failure"
---

## Why This Happens

A single FIFO queue forces sequential processing of all items regardless of their routing key. When items for the same tenant or entity type must be processed in order, all other items queue behind them. Partitioning assigns items to specific partitions by key hash, so multiple workers can process different partitions concurrently while maintaining order within each partition. This is the same principle behind Kafka's partition model.

## Solution 1: Hash-Partitioned Queue Router

```python
import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

@dataclass
class BatchItem:
    item_id: str
    partition_key: str    # tenant_id, user_id, entity_id
    payload: Any
    priority: int = 0
    attempt: int = 0
    created_at: float = field(default_factory=__import__("time").time)

class PartitionedWorkQueue:
    """
    Routes items to one of N partitions by consistent hash of partition_key.
    Each partition is an independent asyncio.Queue, processed by a dedicated worker.
    Maintains ordering within a partition while allowing cross-partition parallelism.
    """

    def __init__(self, num_partitions: int, max_queue_size: int = 1000):
        self._n = num_partitions
        self._partitions: List[asyncio.Queue] = [
            asyncio.Queue(maxsize=max_queue_size) for _ in range(num_partitions)
        ]
        self._counters = [0] * num_partitions

    def partition_for(self, key: str) -> int:
        digest = hashlib.sha256(key.encode()).hexdigest()
        return int(digest[:8], 16) % self._n

    async def enqueue(self, item: BatchItem) -> int:
        partition_id = self.partition_for(item.partition_key)
        await self._partitions[partition_id].put(item)
        self._counters[partition_id] += 1
        return partition_id

    async def dequeue(self, partition_id: int) -> BatchItem:
        return await self._partitions[partition_id].get()

    def task_done(self, partition_id: int) -> None:
        self._partitions[partition_id].task_done()

    def queue_depths(self) -> Dict[int, int]:
        return {i: q.qsize() for i, q in enumerate(self._partitions)}

    def total_enqueued(self) -> int:
        return sum(self._counters)

    def imbalance_ratio(self) -> float:
        depths = list(self.queue_depths().values())
        if not depths or max(depths) == 0:
            return 0.0
        return (max(depths) - min(depths)) / max(depths)


class PartitionedBatchProcessor:
    def __init__(
        self,
        queue: PartitionedWorkQueue,
        process_fn: Callable[[BatchItem], asyncio.Coroutine],
        num_workers_per_partition: int = 1,
    ):
        self._queue = queue
        self._process = process_fn
        self._workers_per_partition = num_workers_per_partition
        self._running = False

    async def start(self) -> None:
        self._running = True
        tasks = []
        for partition_id in range(self._queue._n):
            for _ in range(self._workers_per_partition):
                tasks.append(asyncio.create_task(
                    self._worker_loop(partition_id)
                ))
        await asyncio.gather(*tasks)

    async def _worker_loop(self, partition_id: int) -> None:
        while self._running:
            try:
                item = await asyncio.wait_for(
                    self._queue.dequeue(partition_id), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            try:
                await self._process(item)
            except Exception as exc:
                print(f"[partition:{partition_id}] error processing {item.item_id}: {exc}")
            finally:
                self._queue.task_done(partition_id)
```

## Solution 2: Consistent Hash Ring for Dynamic Partition Rebalancing

```python
import bisect
import hashlib
from typing import Dict, List, Optional

class ConsistentHashRing:
    """
    Consistent hash ring for partition assignment.
    Allows adding/removing partitions with minimal key reassignment.
    Uses virtual nodes for even distribution.
    """

    def __init__(self, virtual_nodes: int = 150):
        self._virtual_nodes = virtual_nodes
        self._ring: Dict[int, str] = {}
        self._sorted_keys: List[int] = []

    def add_partition(self, partition_id: str) -> None:
        for i in range(self._virtual_nodes):
            key = self._hash(f"{partition_id}:{i}")
            self._ring[key] = partition_id
            bisect.insort(self._sorted_keys, key)

    def remove_partition(self, partition_id: str) -> None:
        for i in range(self._virtual_nodes):
            key = self._hash(f"{partition_id}:{i}")
            del self._ring[key]
            idx = bisect.bisect_left(self._sorted_keys, key)
            self._sorted_keys.pop(idx)

    def get_partition(self, item_key: str) -> Optional[str]:
        if not self._ring:
            return None
        key = self._hash(item_key)
        idx = bisect.bisect(self._sorted_keys, key) % len(self._sorted_keys)
        return self._ring[self._sorted_keys[idx]]

    def _hash(self, key: str) -> int:
        return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)

    def partition_distribution(self, sample_keys: List[str]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for key in sample_keys:
            p = self.get_partition(key)
            counts[p] = counts.get(p, 0) + 1
        return counts
```

## Solution 3: Checkpoint-Based Batch Progress Tracker

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

@dataclass
class BatchJobCheckpoint:
    job_id: str
    total_items: int
    completed_ids: Set[str] = field(default_factory=set)
    failed_ids: Dict[str, str] = field(default_factory=dict)  # id -> error
    started_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

class BatchCheckpointStore:
    """
    Tracks per-item completion so restarted jobs resume from where they left off
    rather than reprocessing all items from scratch.
    """

    def __init__(self, redis):
        self._redis = redis

    async def init_job(self, job_id: str, item_ids: List[str]) -> None:
        key = f"batch:{job_id}"
        await self._redis.hmset(key, {
            "total": len(item_ids),
            "started_at": time.time(),
        })
        await self._redis.expire(key, 86400 * 7)  # 7 days

    async def mark_completed(self, job_id: str, item_id: str) -> None:
        await self._redis.sadd(f"batch:{job_id}:completed", item_id)
        await self._redis.hset(f"batch:{job_id}", "updated_at", time.time())

    async def mark_failed(self, job_id: str, item_id: str, error: str) -> None:
        await self._redis.hset(f"batch:{job_id}:failures", item_id, error)

    async def get_pending_ids(self, job_id: str, all_ids: List[str]) -> List[str]:
        """Returns items not yet completed — for resuming a job."""
        completed = await self._redis.smembers(f"batch:{job_id}:completed")
        completed_strs = {m.decode() for m in completed}
        return [item_id for item_id in all_ids if item_id not in completed_strs]

    async def progress(self, job_id: str) -> dict:
        meta = await self._redis.hgetall(f"batch:{job_id}")
        total = int(meta.get(b"total", 0))
        completed = await self._redis.scard(f"batch:{job_id}:completed")
        failed = await self._redis.hlen(f"batch:{job_id}:failures")
        return {
            "job_id": job_id,
            "total": total,
            "completed": completed,
            "failed": failed,
            "pending": total - completed - failed,
            "completion_rate": completed / max(total, 1),
        }
```

## Solution 4: Rate-Limited Partition Worker

```python
import asyncio
import time
from dataclasses import dataclass

@dataclass
class PartitionRateLimit:
    partition_id: int
    max_items_per_second: float
    burst: int = 10

class RateLimitedPartitionWorker:
    """
    Per-partition rate limiting to prevent any single partition from
    consuming all downstream resources (e.g., LLM API rate limits).
    Uses token bucket algorithm per partition.
    """

    def __init__(self, queue: PartitionedWorkQueue, process_fn, rate_limits: dict):
        self._queue = queue
        self._process = process_fn
        self._buckets: dict = {}  # partition_id -> (tokens, last_refill)
        for pid, limit in rate_limits.items():
            self._buckets[pid] = [float(limit.burst), time.monotonic()]

    def _consume_token(self, partition_id: int, rate: float, burst: int) -> float:
        """Returns wait time (0.0 if token available immediately)."""
        tokens, last = self._buckets.get(partition_id, [float(burst), time.monotonic()])
        now = time.monotonic()
        elapsed = now - last
        tokens = min(burst, tokens + elapsed * rate)
        if tokens >= 1.0:
            self._buckets[partition_id] = [tokens - 1.0, now]
            return 0.0
        wait = (1.0 - tokens) / rate
        self._buckets[partition_id] = [tokens, now]
        return wait

    async def worker_loop(self, partition_id: int, rate: float, burst: int) -> None:
        while True:
            wait = self._consume_token(partition_id, rate, burst)
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                item = await asyncio.wait_for(
                    self._queue.dequeue(partition_id), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue
            try:
                await self._process(item)
            except Exception as exc:
                print(f"[partition:{partition_id}] error: {exc}")
            finally:
                self._queue.task_done(partition_id)
```

## Solution 5: Dynamic Partition Scaling

```python
import asyncio
from typing import Dict, List

class DynamicPartitionScaler:
    """
    Monitors queue depths across partitions and spins up additional
    workers for overloaded partitions, scales down idle ones.
    """

    def __init__(
        self,
        queue: PartitionedWorkQueue,
        process_fn,
        min_workers: int = 1,
        max_workers: int = 8,
        scale_up_threshold: int = 50,
        scale_down_threshold: int = 5,
    ):
        self._queue = queue
        self._process = process_fn
        self._min = min_workers
        self._max = max_workers
        self._scale_up = scale_up_threshold
        self._scale_down = scale_down_threshold
        self._worker_tasks: Dict[int, List[asyncio.Task]] = {
            i: [] for i in range(queue._n)
        }

    async def monitor_and_scale(self, interval_seconds: float = 5.0) -> None:
        # Start minimum workers for each partition
        for pid in range(self._queue._n):
            for _ in range(self._min):
                self._add_worker(pid)

        while True:
            await asyncio.sleep(interval_seconds)
            depths = self._queue.queue_depths()
            for pid, depth in depths.items():
                current = len([t for t in self._worker_tasks[pid] if not t.done()])
                if depth > self._scale_up and current < self._max:
                    self._add_worker(pid)
                    print(f"[scaler] partition={pid} scaled up to {current+1} workers (depth={depth})")
                elif depth < self._scale_down and current > self._min:
                    self._remove_worker(pid)

    def _add_worker(self, partition_id: int) -> None:
        task = asyncio.create_task(self._worker_loop(partition_id))
        self._worker_tasks[partition_id].append(task)

    def _remove_worker(self, partition_id: int) -> None:
        tasks = [t for t in self._worker_tasks[partition_id] if not t.done()]
        if tasks:
            tasks[-1].cancel()
            self._worker_tasks[partition_id] = tasks[:-1]

    async def _worker_loop(self, partition_id: int) -> None:
        while True:
            try:
                item = await asyncio.wait_for(
                    self._queue.dequeue(partition_id), timeout=2.0
                )
                await self._process(item)
                self._queue.task_done(partition_id)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
```

## Solution 6: Batch Job Metrics Dashboard

```python
import asyncio
import time
from collections import defaultdict
from typing import Dict

class PartitionedBatchMetrics:
    def __init__(self, queue: PartitionedWorkQueue, checkpoint_store: BatchCheckpointStore):
        self._queue = queue
        self._checkpoints = checkpoint_store
        self._throughput: Dict[int, float] = defaultdict(float)  # partition -> items/sec
        self._processed_counts: Dict[int, int] = defaultdict(int)
        self._last_sample: Dict[int, tuple] = {}

    def record_processed(self, partition_id: int) -> None:
        self._processed_counts[partition_id] += 1

    def throughput_per_partition(self) -> Dict[int, float]:
        now = time.monotonic()
        result = {}
        for pid, count in self._processed_counts.items():
            last_count, last_time = self._last_sample.get(pid, (0, now))
            elapsed = now - last_time
            if elapsed > 0:
                result[pid] = (count - last_count) / elapsed
                self._last_sample[pid] = (count, now)
        return result

    def summary(self, job_id: str = "") -> dict:
        depths = self._queue.queue_depths()
        throughput = self.throughput_per_partition()
        return {
            "job_id": job_id,
            "partition_count": self._queue._n,
            "queue_depths": depths,
            "throughput_per_partition": {k: round(v, 2) for k, v in throughput.items()},
            "total_throughput": round(sum(throughput.values()), 2),
            "imbalance_ratio": round(self._queue.imbalance_ratio(), 3),
            "hottest_partition": max(depths, key=depths.get) if depths else None,
        }
```

## Comparison

| Approach | Ordering Guarantee | Dynamic Scaling | Crash Recovery | Hot Key Handling |
|---|---|---|---|---|
| PartitionedWorkQueue | Within-partition FIFO | No | No | Hash distribution |
| ConsistentHashRing | N/A (routing only) | Yes (add/remove) | N/A | Minimal reassignment |
| BatchCheckpointStore | N/A | N/A | Full resume | N/A |
| RateLimitedPartitionWorker | Within-partition | No | No | Per-partition limits |
| DynamicPartitionScaler | Within-partition | Yes (auto scale-up) | No | Worker scaling |
| PartitionedBatchMetrics | N/A | N/A | N/A | Hottest partition detection |

**Best for production**: Use `PartitionedWorkQueue` as the core, `ConsistentHashRing` if partitions are added/removed dynamically, and `BatchCheckpointStore` for crash-safe resumption on large jobs. Add `DynamicPartitionScaler` to handle uneven partition load automatically and `PartitionedBatchMetrics` to detect hot partitions before they become bottlenecks.
