---
layout: solution
title: "Agent Spawns Too Many Threads — Thread Pool Exhaustion and Resource Starvation"
category: concurrency
description: "The agent spawns a new thread per request. At 500 concurrent sessions, it creates 500 threads. Python's GIL means they don't run in parallel anyway. Thread creation overhead dominates. The OS hits its thread limit and new threads fail with OSError. Worker threads block on I/O and the pool drains. The agent grinds to a halt under load."
tags: [concurrency, threads, thread-pool, asyncio, resource-exhaustion, scalability, performance, python]
---

# Agent Spawns Too Many Threads — Thread Pool Exhaustion and Resource Starvation

## Symptom
- `OSError: [Errno 11] Resource temporarily unavailable` when spawning threads
- `RuntimeError: can't start new thread` under load
- Memory grows linearly with concurrent sessions (each thread uses ~8MB stack)
- High CPU overhead from context switching between hundreds of threads
- Agent handles 10 sessions fine but fails at 50 or 100
- Thread count visible in `ps` or profiler grows unbounded
- Throughput decreases as concurrency increases (opposite of expected)

## Root Cause
`threading.Thread` per request is the wrong model for I/O-bound agents. Python threads are OS threads — each uses 8–16MB of stack memory and requires kernel scheduling. I/O-bound work (API calls, database queries, file reads) spends 90%+ of its time waiting, not running, so more threads don't increase throughput after a point. The Python GIL prevents true parallel CPU execution anyway. The fix is to replace unbounded thread-per-request with a bounded `ThreadPoolExecutor` for blocking code, or better, switch to `asyncio` for all I/O-bound work.

## Fix

### Option 1: Bounded ThreadPoolExecutor — cap threads at a fixed maximum

```python
import concurrent.futures
import threading
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# WRONG — unbounded thread creation:
def BAD_handle_requests(requests: list[dict]) -> list[Any]:
    results = []
    threads = []
    for req in requests:
        t = threading.Thread(target=lambda r: results.append(process(r)), args=(req,))
        t.start()
        threads.append(t)
    for t in threads:
        t.join()
    return results
# At 500 requests: 500 threads × 8MB = 4GB memory, thousands of context switches

# RIGHT — fixed thread pool:
_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=20,              # Hard cap: 20 threads, ever
    thread_name_prefix="agent-worker"
)

def handle_requests_bounded(requests: list[dict]) -> list[Any]:
    """
    Process requests with a bounded thread pool.
    At max_workers=20, excess requests wait in queue instead of creating new threads.
    """
    futures = [_POOL.submit(process, req) for req in requests]
    results = []
    for future in concurrent.futures.as_completed(futures, timeout=60.0):
        try:
            results.append(future.result())
        except Exception as exc:
            logger.error(f"Request failed: {exc}")
            results.append(None)
    return results

def process(request: dict) -> Any:
    """Actual processing logic — runs in a thread pool worker."""
    import anthropic
    client = anthropic.Anthropic()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        messages=[{"role": "user", "content": request["message"]}]
    )
    return response.content[0].text

# Choosing max_workers:
# I/O-bound (API calls): max_workers = 10–50 (threads spend time waiting)
# CPU-bound: max_workers = os.cpu_count() (threads actually run)
# Mixed: max_workers = 2 × os.cpu_count()
import os
RECOMMENDED_MAX_WORKERS = min(32, (os.cpu_count() or 4) * 5)
```

### Option 2: Migrate to asyncio — eliminate threads for I/O-bound work

```python
import asyncio
import anthropic
import logging
from typing import Any

logger = logging.getLogger(__name__)

# asyncio handles thousands of concurrent connections with a single thread.
# No thread creation overhead, no GIL contention, minimal memory per "task".

async def process_request(request: dict, semaphore: asyncio.Semaphore) -> Any:
    """
    Async processing — yields control during API calls so other tasks run.
    semaphore limits concurrent Claude calls without blocking threads.
    """
    async with semaphore:
        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            messages=[{"role": "user", "content": request["message"]}]
        )
        return response.content[0].text

async def handle_requests_async(requests: list[dict], max_concurrent: int = 20) -> list[Any]:
    """
    Process all requests concurrently with a semaphore-bounded concurrency.
    500 requests → 20 at a time, but only 1 thread total.
    Memory: ~10KB per asyncio task (vs 8MB per thread).
    """
    semaphore = asyncio.Semaphore(max_concurrent)
    tasks = [process_request(req, semaphore) for req in requests]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            logger.error(f"Request {i} failed: {result}")
            results[i] = None

    return results

# Run from sync context:
def handle_requests(requests: list[dict]) -> list[Any]:
    return asyncio.run(handle_requests_async(requests, max_concurrent=20))

# asyncio concurrency capacity comparison:
# threading.Thread: 100 threads → ~800MB stack memory
# asyncio tasks:    100 tasks   → ~1MB memory
# asyncio tasks:    10,000 tasks → ~100MB memory (10,000× more than threads at same memory)
```

### Option 3: Thread pool per resource type — separate pools with independent limits

```python
import concurrent.futures
import os
import logging
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)
T = TypeVar("T")

class ResourceSeparatedPools:
    """
    Separate thread pools per resource type.
    Prevents one slow resource from starving all others.
    """

    def __init__(self):
        cpu_count = os.cpu_count() or 4

        # LLM API calls — I/O bound, can have many concurrent:
        self.llm_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=15,
            thread_name_prefix="llm"
        )

        # Database queries — I/O bound but often have connection limits:
        self.db_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=10,
            thread_name_prefix="db"
        )

        # CPU-bound work (embeddings, text processing):
        self.cpu_pool = concurrent.futures.ProcessPoolExecutor(
            max_workers=cpu_count,  # One process per CPU core
        )

        # File I/O — usually fast but can block:
        self.io_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=5,
            thread_name_prefix="io"
        )

    def submit_llm(self, fn: Callable[..., T], *args, **kwargs) -> concurrent.futures.Future:
        return self.llm_pool.submit(fn, *args, **kwargs)

    def submit_db(self, fn: Callable[..., T], *args, **kwargs) -> concurrent.futures.Future:
        return self.db_pool.submit(fn, *args, **kwargs)

    def submit_cpu(self, fn: Callable[..., T], *args, **kwargs) -> concurrent.futures.Future:
        return self.cpu_pool.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True):
        self.llm_pool.shutdown(wait=wait)
        self.db_pool.shutdown(wait=wait)
        self.cpu_pool.shutdown(wait=wait)
        self.io_pool.shutdown(wait=wait)

pools = ResourceSeparatedPools()

def full_pipeline(question: str) -> str:
    """
    Pipeline using separate pools per resource type.
    A slow DB query doesn't block LLM calls.
    """
    import anthropic

    # Step 1: Fetch context from DB (db_pool)
    def fetch_context():
        import sqlite3
        conn = sqlite3.connect("/app/knowledge.db")
        rows = conn.execute("SELECT content FROM docs WHERE topic LIKE ?", (f"%{question}%",)).fetchall()
        conn.close()
        return "\n".join(r[0] for r in rows[:3])

    context_future = pools.submit_db(fetch_context)

    # Step 2: Wait for context, then call LLM (llm_pool)
    context = context_future.result(timeout=10.0)

    def call_llm():
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=f"Context:\n{context}" if context else "",
            messages=[{"role": "user", "content": question}]
        )
        return response.content[0].text

    llm_future = pools.submit_llm(call_llm)
    return llm_future.result(timeout=30.0)
```

### Option 4: Thread pool monitoring — detect and alert on pool exhaustion

```python
import concurrent.futures
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Callable, Any

logger = logging.getLogger(__name__)

@dataclass
class PoolStats:
    submitted: int = 0
    completed: int = 0
    failed: int = 0
    rejected: int = 0
    max_queue_depth: int = 0
    current_queue_depth: int = 0

class MonitoredThreadPool:
    """
    ThreadPoolExecutor with monitoring, stats, and backpressure.
    Rejects new work when queue grows too deep instead of unbounded growth.
    """

    def __init__(
        self,
        max_workers: int = 20,
        max_queue_depth: int = 200,   # Reject new work beyond this
        thread_name_prefix: str = "agent"
    ):
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix=thread_name_prefix
        )
        self._max_queue = max_queue_depth
        self._stats = PoolStats()
        self._lock = threading.Lock()
        self._active_count = 0
        self._max_workers = max_workers

    @property
    def stats(self) -> PoolStats:
        return self._stats

    @property
    def utilization(self) -> float:
        """Fraction of workers currently busy."""
        return self._active_count / self._max_workers

    def submit(self, fn: Callable, *args, **kwargs) -> concurrent.futures.Future | None:
        """
        Submit work. Returns None if pool is overloaded (backpressure).
        Caller should handle None gracefully (queue externally or return 503).
        """
        with self._lock:
            queue_depth = self._stats.submitted - self._stats.completed - self._stats.failed
            self._stats.max_queue_depth = max(self._stats.max_queue_depth, queue_depth)
            self._stats.current_queue_depth = queue_depth

            if queue_depth >= self._max_queue:
                self._stats.rejected += 1
                logger.warning(
                    f"Pool overloaded: queue={queue_depth}/{self._max_queue}, "
                    f"workers={self._active_count}/{self._max_workers}. Rejecting request."
                )
                return None

            self._stats.submitted += 1
            self._active_count = min(self._active_count + 1, self._max_workers)

        def wrapped():
            try:
                result = fn(*args, **kwargs)
                with self._lock:
                    self._stats.completed += 1
                    self._active_count = max(0, self._active_count - 1)
                return result
            except Exception as exc:
                with self._lock:
                    self._stats.failed += 1
                    self._active_count = max(0, self._active_count - 1)
                raise

        return self._pool.submit(wrapped)

    def log_stats(self):
        s = self._stats
        logger.info(
            f"Pool stats: submitted={s.submitted}, completed={s.completed}, "
            f"failed={s.failed}, rejected={s.rejected}, "
            f"queue={s.current_queue_depth}/{self._max_queue}, "
            f"utilization={self.utilization:.0%}"
        )
        if self.utilization > 0.85:
            logger.warning("Thread pool utilization > 85% — consider increasing max_workers or reducing load")

monitored_pool = MonitoredThreadPool(max_workers=20, max_queue_depth=100)
```

### Option 5: Work stealing queue — balance load across workers

```python
import queue
import threading
import time
import logging
from typing import Any, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

@dataclass
class WorkItem:
    fn: Callable
    args: tuple
    kwargs: dict
    result_future: "concurrent.futures.Future"
    submitted_at: float = field(default_factory=time.monotonic)

class BoundedWorkerPool:
    """
    Worker pool with bounded queue and work-stealing.
    Workers share a single queue — naturally load-balances without overhead.
    Workers exit when idle, freeing system resources.
    """

    def __init__(
        self,
        min_workers: int = 2,
        max_workers: int = 20,
        queue_size: int = 500,
        idle_timeout: float = 30.0  # Workers exit after 30s idle
    ):
        self._min_workers = min_workers
        self._max_workers = max_workers
        self._idle_timeout = idle_timeout
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._workers: set[threading.Thread] = set()
        self._lock = threading.Lock()
        self._shutdown = False

        # Start minimum workers:
        for _ in range(min_workers):
            self._start_worker()

    def _start_worker(self):
        t = threading.Thread(target=self._worker_loop, daemon=True)
        t.start()
        self._workers.add(t)

    def _worker_loop(self):
        """Worker loop — exits after idle_timeout with no work."""
        while not self._shutdown:
            try:
                item: WorkItem = self._queue.get(timeout=self._idle_timeout)
            except queue.Empty:
                # Idle timeout — exit if above min_workers
                with self._lock:
                    if len(self._workers) > self._min_workers:
                        self._workers.discard(threading.current_thread())
                        return
                continue

            try:
                result = item.fn(*item.args, **item.kwargs)
                if not item.result_future.done():
                    item.result_future.set_result(result)
            except Exception as exc:
                if not item.result_future.done():
                    item.result_future.set_exception(exc)
            finally:
                self._queue.task_done()

    def submit(self, fn: Callable, *args, **kwargs) -> "concurrent.futures.Future":
        import concurrent.futures
        future = concurrent.futures.Future()
        item = WorkItem(fn=fn, args=args, kwargs=kwargs, result_future=future)

        # Scale up workers if needed (up to max):
        with self._lock:
            if self._queue.qsize() > 0 and len(self._workers) < self._max_workers:
                self._start_worker()
                logger.debug(f"Scaled up: {len(self._workers)}/{self._max_workers} workers")

        try:
            self._queue.put_nowait(item)
        except queue.Full:
            future.set_exception(RuntimeError("Worker pool queue full — request rejected"))

        return future

    def shutdown(self):
        self._shutdown = True
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

pool = BoundedWorkerPool(min_workers=2, max_workers=20, queue_size=200)
```

### Option 6: asyncio + run_in_executor bridge — mix sync and async cleanly

```python
import asyncio
import concurrent.futures
import anthropic
import logging
from typing import Any

logger = logging.getLogger(__name__)

# A shared thread pool for legacy blocking code:
_BLOCKING_POOL = concurrent.futures.ThreadPoolExecutor(
    max_workers=10,
    thread_name_prefix="blocking"
)

async def run_blocking(fn, *args, **kwargs) -> Any:
    """Run a blocking function in the thread pool without creating a new thread."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(_BLOCKING_POOL, lambda: fn(*args, **kwargs))

async def process_session(session_id: str, message: str) -> str:
    """
    Fully async session processing.
    Blocking DB calls go through run_blocking — no new threads created per session.
    """
    # Blocking DB call — uses thread pool, not a new thread:
    def fetch_history():
        import sqlite3
        conn = sqlite3.connect("/app/sessions.db")
        rows = conn.execute(
            "SELECT role, content FROM messages WHERE session_id=? ORDER BY id DESC LIMIT 10",
            (session_id,)
        ).fetchall()
        conn.close()
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]

    history = await run_blocking(fetch_history)

    # Async LLM call — no thread at all:
    client = anthropic.AsyncAnthropic()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=history + [{"role": "user", "content": message}]
    )
    reply = response.content[0].text

    # Blocking DB write — uses thread pool:
    def save_messages():
        import sqlite3
        conn = sqlite3.connect("/app/sessions.db")
        conn.execute("INSERT INTO messages VALUES (?, 'user', ?)", (session_id, message))
        conn.execute("INSERT INTO messages VALUES (?, 'assistant', ?)", (session_id, reply))
        conn.commit()
        conn.close()

    await run_blocking(save_messages)
    return reply

async def handle_concurrent_sessions(sessions: list[dict]) -> list[str]:
    """
    1000 concurrent sessions → 1 event loop thread + 10 DB threads.
    vs. naive approach: 1000 OS threads × 8MB = 8GB memory.
    """
    semaphore = asyncio.Semaphore(50)  # Max 50 concurrent LLM calls

    async def bounded_process(session):
        async with semaphore:
            return await process_session(session["id"], session["message"])

    return await asyncio.gather(*[bounded_process(s) for s in sessions])
```

## Threading Model Comparison

| Approach | Max Concurrency | Memory per Unit | GIL Limited | Best For |
|---|---|---|---|---|
| `threading.Thread` per request | ~100–500 (OS limit) | ~8MB per thread | Yes | Legacy blocking code only |
| `ThreadPoolExecutor(max_workers=N)` | N (bounded) | ~8MB per active thread | Yes | Bounded blocking I/O |
| `asyncio` + `AsyncAnthropic` | 10,000+ | ~10KB per task | No (for I/O) | All I/O-bound work |
| `asyncio` + `run_in_executor` | Mixed | Per thread pool | Partial | Mix of blocking + async |
| `ProcessPoolExecutor` | CPU count | ~30MB per process | No | CPU-bound work |

## Recommended max_workers by Workload

| Workload | max_workers Formula | Typical Value |
|---|---|---|
| Pure LLM API calls (async preferred) | 10–20 if using threads | 15 |
| Mixed I/O (DB + API) | 2–4 × cpu_count | 16–32 |
| CPU-bound (embedding, processing) | cpu_count | 4–8 |
| File I/O | 4–8 | 5 |

## Expected Token Savings
N/A — thread exhaustion blocks all API calls, so there are no token savings to measure.
Thread-safe bounded pool: agents serve 10× more concurrent users at the same memory footprint.

## Environment
- Any Python agent serving multiple concurrent users; thread exhaustion is most common when: migrating from single-user to multi-user deployment, adding background tasks, or integrating legacy blocking libraries into an async agent; the symptom appears suddenly at a specific concurrency threshold (the thread limit) rather than gradually
- Source: direct experience; `RuntimeError: can't start new thread` is the most common production failure for agents that passed load testing at low concurrency but were deployed without a bounded thread pool
