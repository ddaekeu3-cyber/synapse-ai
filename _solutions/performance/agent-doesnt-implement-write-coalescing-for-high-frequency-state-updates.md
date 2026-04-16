---
title: "Agent Doesn't Implement Write Coalescing for High-Frequency State Updates"
description: "Agents that persist state after every tool call or token emit one DB write per event, overwhelming the database under load. Implement write coalescing to buffer rapid updates and flush them in batches, drastically reducing write throughput while preserving eventual consistency."
date: 2026-04-16
difficulty: intermediate
category: performance
slug: agent-doesnt-implement-write-coalescing-for-high-frequency-state-updates
tags: [write-coalescing, batching, database, performance, state-management, buffering]
symptoms:
  - "Database write IOPS spike to thousands per second during active agent sessions"
  - "Each token or tool-call result triggers an individual INSERT or UPDATE"
  - "DB connection pool exhausted under concurrent agent load"
  - "p99 write latency blooms because DB is saturated with small writes"
  - "Agent state table grows by millions of rows per hour with mostly redundant updates"
---

## Why This Happens

Agents produce frequent state changes: token counts, partial responses, tool call results, memory updates. Naive implementations persist each change immediately, treating the database as an event stream. Under any meaningful concurrency, this produces a write storm. Write coalescing buffers these changes in memory and flushes them to the database either on a time interval, when the buffer is full, or when an explicit sync is requested — reducing DB load by orders of magnitude.

## Solution 1: Time-Windowed Write Coalescer

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

@dataclass
class PendingWrite:
    key: str
    value: Any
    queued_at: float = field(default_factory=time.monotonic)

class TimeWindowedWriteCoalescer:
    """
    Buffers writes per key. Only the latest write per key survives in the
    buffer (newer overwrites older). Flushes to the database at a fixed
    interval or when max buffer size is reached.
    """

    def __init__(
        self,
        flush_fn: Callable[[Dict[str, Any]], asyncio.Future],
        flush_interval_seconds: float = 1.0,
        max_buffer_size: int = 500,
    ):
        self._flush = flush_fn
        self._interval = flush_interval_seconds
        self._max_size = max_buffer_size
        self._buffer: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None

    async def write(self, key: str, value: Any) -> None:
        async with self._lock:
            self._buffer[key] = value
            if len(self._buffer) >= self._max_size:
                await self._do_flush()

    async def _do_flush(self) -> None:
        if not self._buffer:
            return
        snapshot = dict(self._buffer)
        self._buffer.clear()
        try:
            await self._flush(snapshot)
        except Exception as exc:
            print(f"[write_coalescer] flush error: {exc}")
            # Re-queue failed writes (last-write semantics preserved)
            async with self._lock:
                for k, v in snapshot.items():
                    self._buffer.setdefault(k, v)

    async def flush_now(self) -> None:
        async with self._lock:
            await self._do_flush()

    async def run_flush_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            async with self._lock:
                await self._do_flush()

    def start(self) -> asyncio.Task:
        self._flush_task = asyncio.create_task(self.run_flush_loop())
        return self._flush_task

    async def stop(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
        await self.flush_now()
```

## Solution 2: Debounce Write Coalescer (Per-Key Delay)

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional

class DebounceWriteCoalescer:
    """
    Delays each write by a debounce window. If more writes arrive for the
    same key within the window, the timer resets. Only fires the write
    after `debounce_seconds` of inactivity for that key.

    Use for: session state, streaming token buffers, frequent counter updates.
    """

    def __init__(
        self,
        flush_fn: Callable[[str, Any], asyncio.Future],
        debounce_seconds: float = 0.5,
        max_delay_seconds: float = 5.0,
    ):
        self._flush = flush_fn
        self._debounce = debounce_seconds
        self._max_delay = max_delay_seconds
        self._pending: Dict[str, Any] = {}
        self._timers: Dict[str, asyncio.TimerHandle] = {}
        self._first_write: Dict[str, float] = {}
        self._loop = asyncio.get_event_loop()

    def write(self, key: str, value: Any) -> None:
        self._pending[key] = value
        now = time.monotonic()

        # Track first write time for max-delay enforcement
        if key not in self._first_write:
            self._first_write[key] = now

        # Cancel existing debounce timer
        if key in self._timers:
            self._timers[key].cancel()

        # Enforce max delay: flush immediately if we've waited too long
        elapsed = now - self._first_write[key]
        if elapsed >= self._max_delay:
            self._loop.create_task(self._flush_key(key))
            return

        remaining = min(self._debounce, self._max_delay - elapsed)
        self._timers[key] = self._loop.call_later(
            remaining, lambda k=key: self._loop.create_task(self._flush_key(k))
        )

    async def _flush_key(self, key: str) -> None:
        value = self._pending.pop(key, None)
        self._timers.pop(key, None)
        self._first_write.pop(key, None)
        if value is not None:
            try:
                await self._flush(key, value)
            except Exception as exc:
                print(f"[debounce_coalescer] error flushing key={key}: {exc}")

    async def flush_all(self) -> None:
        keys = list(self._pending.keys())
        await asyncio.gather(*[self._flush_key(k) for k in keys])
```

## Solution 3: Batch Writer with Priority Queue

```python
import asyncio
import heapq
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

@dataclass(order=True)
class WriteEntry:
    priority: int          # lower = higher priority
    timestamp: float = field(compare=False)
    key: str = field(compare=False)
    value: Any = field(compare=False)

class PriorityBatchWriter:
    """
    High-priority writes (e.g., user-visible state, billing counters) flush
    immediately; low-priority writes (logs, debug state) are batched.
    """

    HIGH = 0
    NORMAL = 1
    LOW = 2

    def __init__(
        self,
        batch_flush_fn: Callable[[List[tuple]], asyncio.Future],
        high_flush_fn: Callable[[str, Any], asyncio.Future],
        batch_size: int = 100,
        batch_interval: float = 2.0,
    ):
        self._batch_flush = batch_flush_fn
        self._high_flush = high_flush_fn
        self._batch_size = batch_size
        self._batch_interval = batch_interval
        self._queue: List[WriteEntry] = []
        self._latest: Dict[str, WriteEntry] = {}  # dedup by key
        self._lock = asyncio.Lock()

    async def write(self, key: str, value: Any, priority: int = 1) -> None:
        if priority == self.HIGH:
            await self._high_flush(key, value)
            return

        entry = WriteEntry(priority=priority, timestamp=time.monotonic(), key=key, value=value)
        async with self._lock:
            self._latest[key] = entry
            if len(self._latest) >= self._batch_size:
                await self._flush_batch()

    async def _flush_batch(self) -> None:
        if not self._latest:
            return
        entries = list(self._latest.values())
        self._latest.clear()
        batch = [(e.key, e.value) for e in sorted(entries)]
        try:
            await self._batch_flush(batch)
        except Exception as exc:
            print(f"[priority_batch_writer] flush error: {exc}")

    async def run_loop(self) -> None:
        while True:
            await asyncio.sleep(self._batch_interval)
            async with self._lock:
                await self._flush_batch()
```

## Solution 4: Write-Behind Cache (Async Write-Through)

```python
import asyncio
from typing import Any, Callable, Dict, Optional

class WriteBehindCache:
    """
    Reads are served from an in-process dict (hot cache).
    Writes update the cache immediately (read-your-writes) but
    persist to the database asynchronously in a background queue.
    Durable on shutdown: drains the queue before exiting.
    """

    def __init__(
        self,
        persist_fn: Callable[[str, Any], asyncio.Future],
        max_queue_size: int = 1000,
        num_workers: int = 4,
    ):
        self._persist = persist_fn
        self._cache: Dict[str, Any] = {}
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_queue_size)
        self._workers: list = []
        self._num_workers = num_workers

    def get(self, key: str) -> Optional[Any]:
        return self._cache.get(key)

    async def set(self, key: str, value: Any) -> None:
        self._cache[key] = value  # synchronous hot-path
        try:
            self._queue.put_nowait((key, value))
        except asyncio.QueueFull:
            # Queue full: drop oldest, insert new (coalescing)
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            await self._queue.put((key, value))

    async def _worker(self) -> None:
        while True:
            key, value = await self._queue.get()
            try:
                await self._persist(key, value)
            except Exception as exc:
                print(f"[write_behind_cache] persist error key={key}: {exc}")
            finally:
                self._queue.task_done()

    def start(self) -> None:
        for _ in range(self._num_workers):
            self._workers.append(asyncio.create_task(self._worker()))

    async def shutdown(self) -> None:
        await self._queue.join()
        for w in self._workers:
            w.cancel()
```

## Solution 5: Delta Coalescer (Accumulate Numeric Increments)

```python
import asyncio
from collections import defaultdict
from typing import Callable, Dict

class DeltaCoalescer:
    """
    For numeric counters: accumulates increments in memory and flushes
    the total delta to the DB. Converts N individual UPDATE counter = counter + 1
    calls into a single UPDATE counter = counter + N.
    """

    def __init__(
        self,
        flush_fn: Callable[[Dict[str, float]], asyncio.Future],
        flush_interval_seconds: float = 5.0,
    ):
        self._flush = flush_fn
        self._interval = flush_interval_seconds
        self._deltas: Dict[str, float] = defaultdict(float)
        self._lock = asyncio.Lock()

    async def increment(self, key: str, delta: float = 1.0) -> None:
        async with self._lock:
            self._deltas[key] += delta

    async def _flush_now(self) -> None:
        async with self._lock:
            if not self._deltas:
                return
            snapshot = dict(self._deltas)
            self._deltas.clear()
        try:
            await self._flush(snapshot)
        except Exception as exc:
            print(f"[delta_coalescer] flush error: {exc}")
            async with self._lock:
                for k, v in snapshot.items():
                    self._deltas[k] += v  # re-queue

    async def run_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._flush_now()

    async def flush(self) -> None:
        await self._flush_now()

# Example: agent token usage accumulation
async def flush_token_usage(deltas: Dict[str, float]) -> None:
    # Single SQL: UPDATE token_usage SET tokens = tokens + $delta WHERE session_id = $id
    pass  # replace with real DB call

token_coalescer = DeltaCoalescer(flush_fn=flush_token_usage, flush_interval_seconds=10.0)

# Called on every LLM response chunk — no DB write per chunk:
async def on_token_emitted(session_id: str) -> None:
    await token_coalescer.increment(f"session:{session_id}:tokens")
    await token_coalescer.increment(f"tenant:{session_id.split(':')[0]}:tokens")
```

## Solution 6: Write Coalescing Metrics Exporter

```python
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class CoalescerStats:
    writes_received: int = 0
    writes_flushed: int = 0
    flushes_performed: int = 0
    keys_coalesced: int = 0  # writes_received - writes_flushed
    last_flush_at: float = 0.0
    avg_batch_size: float = 0.0

class InstrumentedWriteCoalescer(TimeWindowedWriteCoalescer):
    """Adds metrics to the base write coalescer."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._stats = CoalescerStats()

    async def write(self, key: str, value) -> None:
        self._stats.writes_received += 1
        await super().write(key, value)

    async def _do_flush(self) -> None:
        batch_size = len(self._buffer)
        if batch_size == 0:
            return
        await super()._do_flush()
        self._stats.writes_flushed += batch_size
        self._stats.flushes_performed += 1
        self._stats.last_flush_at = time.time()
        total_coalesced = self._stats.writes_received - self._stats.writes_flushed
        self._stats.keys_coalesced = max(0, total_coalesced)
        # EMA of batch size
        self._stats.avg_batch_size = (
            self._stats.avg_batch_size * 0.9 + batch_size * 0.1
        )

    def stats(self) -> dict:
        coalesce_ratio = (
            1.0 - self._stats.writes_flushed / max(self._stats.writes_received, 1)
        )
        return {
            "writes_received": self._stats.writes_received,
            "writes_flushed": self._stats.writes_flushed,
            "coalesce_ratio": coalesce_ratio,
            "flushes_performed": self._stats.flushes_performed,
            "avg_batch_size": self._stats.avg_batch_size,
        }

    async def log_stats_loop(self, interval: float = 60.0) -> None:
        while True:
            await asyncio.sleep(interval)
            s = self.stats()
            print(
                f"[write_coalescer] received={s['writes_received']} "
                f"flushed={s['writes_flushed']} "
                f"coalesce_ratio={s['coalesce_ratio']:.1%} "
                f"avg_batch={s['avg_batch_size']:.1f}"
            )
```

## Comparison

| Approach | Key Coalescing | Read-Your-Writes | Priority Support | Durability |
|---|---|---|---|---|
| TimeWindowedWriteCoalescer | Yes (last write wins) | No | No | Flush on stop |
| DebounceWriteCoalescer | Yes (per-key timer) | No | No | Flush all on shutdown |
| PriorityBatchWriter | Yes | No | Yes (3 levels) | High-priority immediate |
| WriteBehindCache | No (all writes queued) | Yes (hot cache) | No | Queue drain on shutdown |
| DeltaCoalescer | Yes (sum deltas) | No | No | Flush on stop |
| InstrumentedWriteCoalescer | Yes + metrics | No | No | Flush on stop |

**Best for production**: Use `WriteBehindCache` for any state that must be read-your-writes consistent (session data, conversation history). Use `DeltaCoalescer` for counters and metrics. Use `TimeWindowedWriteCoalescer` or `DebounceWriteCoalescer` for agent state blobs where eventual persistence within 1–5 seconds is acceptable. Wrap with `InstrumentedWriteCoalescer` to track coalesce ratio in production.
