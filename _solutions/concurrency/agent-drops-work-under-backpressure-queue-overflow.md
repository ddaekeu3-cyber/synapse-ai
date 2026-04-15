---
layout: solution
title: "Agent Drops Work Under Backpressure — Queue Overflow Loses Requests"
category: concurrency
description: "When request volume spikes, the agent's internal queue fills up. New requests are silently dropped, or the caller gets a 500 error with no retry guidance. Users lose requests they thought were submitted. The fix is to implement explicit backpressure signals, bounded queues with overflow strategies, and visible queue depth metrics so operators can scale before work is lost."
tags: [concurrency, backpressure, queue, overflow, reliability, load-shedding, rate-limiting, capacity]
---

# Agent Drops Work Under Backpressure — Queue Overflow Loses Requests

## Symptom
- Under load, some agent requests silently disappear — no response, no error
- Queue grows unbounded until process OOMs, then all queued work is lost
- Caller submits 100 tasks, only 73 complete — 27 were silently discarded
- During a traffic spike, new requests get immediate 500 errors with no retry-after hint
- Metrics show queue depth of 0 at all times — but requests are clearly being lost
- Backpressure only discovered when a downstream database gets overwhelmed by queued writes

## Root Cause
Unbounded queues accept all work until memory is exhausted. Bounded queues configured with `drop` or `raise` overflow strategy discard excess work without notifying callers. Neither communicates load to producers so they can slow down. The fix is to: (1) use bounded queues with explicit overflow handling, (2) return backpressure signals (HTTP 429/503, `Retry-After`) to callers, (3) monitor queue depth, and (4) implement load-shedding with priority to protect critical work.

## Fix

### Option 1: Bounded asyncio queue with explicit backpressure response

```python
import anthropic
import asyncio
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

@dataclass
class AgentRequest:
    request_id: str
    message: str
    submitted_at: float = 0.0

    def __post_init__(self):
        if not self.submitted_at:
            self.submitted_at = time.monotonic()


class BoundedAgentQueue:
    """
    Bounded queue for agent requests.
    Returns explicit backpressure signals when full instead of dropping silently.
    """
    def __init__(self, max_size: int = 100, max_workers: int = 5):
        self._queue: asyncio.Queue[AgentRequest] = asyncio.Queue(maxsize=max_size)
        self._max_workers = max_workers
        self._workers: list[asyncio.Task] = []
        self._processed = 0
        self._dropped = 0
        self._started = False

    async def start(self):
        """Start worker pool."""
        self._started = True
        self._workers = [
            asyncio.create_task(self._worker(i))
            for i in range(self._max_workers)
        ]
        logger.info(f"Queue started: max_size={self._queue.maxsize}, workers={self._max_workers}")

    async def submit(self, request: AgentRequest) -> dict:
        """
        Submit a request. Returns immediately.
        If queue is full, returns a 'backpressure' response instead of dropping.
        """
        if self._queue.full():
            self._dropped += 1
            queue_depth = self._queue.qsize()
            logger.warning(f"Queue full (depth={queue_depth}): rejecting {request.request_id}")
            return {
                "status": "rejected",
                "reason": "queue_full",
                "queue_depth": queue_depth,
                "retry_after_seconds": self._estimate_retry_after(),
                "request_id": request.request_id
            }

        await self._queue.put(request)
        return {
            "status": "queued",
            "queue_depth": self._queue.qsize(),
            "request_id": request.request_id
        }

    def _estimate_retry_after(self) -> int:
        """Estimate how many seconds until a slot opens (based on throughput)."""
        if self._processed == 0:
            return 5
        elapsed = time.monotonic() - (getattr(self, '_start_time', time.monotonic()))
        if elapsed < 1:
            return 5
        throughput_per_second = self._processed / elapsed
        return max(1, int(self._queue.maxsize / max(throughput_per_second, 0.1)))

    async def _worker(self, worker_id: int):
        logger.info(f"Worker {worker_id} started")
        while True:
            try:
                request = await asyncio.wait_for(
                    self._queue.get(), timeout=30.0
                )
                wait_time = time.monotonic() - request.submitted_at
                logger.debug(f"Worker {worker_id}: processing {request.request_id} (waited {wait_time:.2f}s)")

                await self._process_request(request)
                self._processed += 1
                self._queue.task_done()

            except asyncio.TimeoutError:
                continue  # no work, keep waiting
            except asyncio.CancelledError:
                break

    async def _process_request(self, request: AgentRequest):
        """Process one agent request."""
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": request.message}]
        )
        logger.info(f"Completed {request.request_id}: {response.content[0].text[:50]}")

    @property
    def stats(self) -> dict:
        return {
            "queue_depth": self._queue.qsize(),
            "max_size": self._queue.maxsize,
            "processed": self._processed,
            "dropped": self._dropped,
            "utilization": self._queue.qsize() / self._queue.maxsize
        }

    async def stop(self):
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)


# FastAPI integration:
# queue = BoundedAgentQueue(max_size=100, max_workers=5)
#
# @app.on_event("startup")
# async def startup():
#     await queue.start()
#
# @app.post("/process")
# async def process(body: RequestBody):
#     result = await queue.submit(AgentRequest(request_id=str(uuid4()), message=body.message))
#     if result["status"] == "rejected":
#         raise HTTPException(
#             status_code=503,
#             detail=result,
#             headers={"Retry-After": str(result["retry_after_seconds"])}
#         )
#     return result
```

### Option 2: Priority queue with load shedding — protect critical work

```python
import anthropic
import asyncio
import heapq
import time
import logging
from dataclasses import dataclass, field
from enum import IntEnum

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    CRITICAL = 0    # always processed (payments, safety-critical)
    HIGH = 1        # processed unless severely overloaded
    NORMAL = 2      # shed first under load
    LOW = 3         # shed when queue > 50% full

@dataclass(order=True)
class PriorityRequest:
    priority: Priority
    submitted_at: float = field(compare=False, default_factory=time.monotonic)
    request_id: str = field(compare=False, default="")
    message: str = field(compare=False, default="")


class PriorityAgentQueue:
    """
    Priority queue that sheds low-priority work first under load.
    Critical work always gets through; low-priority work is rejected early.
    """
    def __init__(self, max_size: int = 200, max_workers: int = 8):
        self._heap: list[PriorityRequest] = []
        self._max_size = max_size
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Event()
        self._max_workers = max_workers
        self._processed = 0
        self._shed = 0

    def _shed_threshold_for(self, priority: Priority) -> float:
        """Return the queue utilization % at which this priority starts being shed."""
        return {
            Priority.CRITICAL: 1.0,   # shed only when 100% full (never)
            Priority.HIGH: 0.90,      # shed when >90% full
            Priority.NORMAL: 0.70,    # shed when >70% full
            Priority.LOW: 0.50,       # shed when >50% full
        }[priority]

    async def submit(self, request: PriorityRequest) -> dict:
        async with self._lock:
            utilization = len(self._heap) / self._max_size
            threshold = self._shed_threshold_for(request.priority)

            if utilization >= threshold:
                self._shed += 1
                logger.warning(
                    f"Load shedding {request.priority.name} request {request.request_id} "
                    f"(utilization={utilization:.0%} >= threshold={threshold:.0%})"
                )
                return {
                    "status": "shed",
                    "priority": request.priority.name,
                    "utilization": utilization,
                    "retry_after_seconds": max(5, int(utilization * 30))
                }

            heapq.heappush(self._heap, request)
            self._not_empty.set()
            return {
                "status": "queued",
                "priority": request.priority.name,
                "queue_depth": len(self._heap)
            }

    async def _get_next(self) -> PriorityRequest:
        while True:
            async with self._lock:
                if self._heap:
                    return heapq.heappop(self._heap)
                self._not_empty.clear()
            await self._not_empty.wait()

    async def _worker(self, worker_id: int):
        while True:
            try:
                request = await asyncio.wait_for(self._get_next(), timeout=10.0)
                wait = time.monotonic() - request.submitted_at
                logger.debug(
                    f"Worker {worker_id}: {request.priority.name} {request.request_id} "
                    f"(waited {wait:.2f}s)"
                )
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{"role": "user", "content": request.message}]
                )
                self._processed += 1
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    @property
    def stats(self) -> dict:
        return {
            "queue_depth": len(self._heap),
            "utilization": len(self._heap) / self._max_size,
            "processed": self._processed,
            "shed": self._shed,
        }
```

### Option 3: Token bucket rate limiter — prevent queue overflow at the source

```python
import anthropic
import asyncio
import time
import logging

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

class TokenBucketLimiter:
    """
    Token bucket rate limiter for agent submissions.
    Prevents queue overflow by limiting intake rate.
    """
    def __init__(self, rate_per_second: float, burst: int):
        self.rate = rate_per_second
        self.burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self, cost: int = 1) -> tuple[bool, float]:
        """
        Try to acquire tokens.
        Returns (allowed, retry_after_seconds).
        """
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
            self._last_refill = now

            if self._tokens >= cost:
                self._tokens -= cost
                return True, 0.0
            else:
                # Time until enough tokens available:
                needed = cost - self._tokens
                retry_after = needed / self.rate
                return False, retry_after


class RateLimitedAgentPool:
    """
    Agent pool with token bucket rate limiting at the entry point.
    Callers receive explicit Retry-After guidance instead of dropped requests.
    """
    def __init__(
        self,
        requests_per_second: float = 10.0,
        burst: int = 20,
        max_concurrent: int = 5
    ):
        self._limiter = TokenBucketLimiter(requests_per_second, burst)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._submitted = 0
        self._rejected = 0

    async def submit(self, message: str) -> dict:
        """Submit a request — returns 429 with Retry-After if rate limited."""
        allowed, retry_after = await self._limiter.acquire()

        if not allowed:
            self._rejected += 1
            logger.info(f"Rate limited: retry in {retry_after:.1f}s")
            return {
                "status": "rate_limited",
                "retry_after_seconds": retry_after,
                "http_status": 429
            }

        self._submitted += 1
        async with self._semaphore:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": message}]
            )
            return {
                "status": "ok",
                "response": response.content[0].text
            }

    @property
    def stats(self) -> dict:
        return {
            "submitted": self._submitted,
            "rejected": self._rejected,
            "rejection_rate": self._rejected / max(1, self._submitted + self._rejected)
        }
```

### Option 4: Persistent queue — survive process restarts without losing work

```python
import anthropic
import asyncio
import json
import time
import sqlite3
import uuid
import logging
from contextlib import contextmanager

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

class DurableAgentQueue:
    """
    SQLite-backed queue — work survives process restarts.
    Combined with bounded capacity and explicit backpressure signals.
    """
    def __init__(self, db_path: str = "/tmp/agent_queue.db", max_size: int = 500):
        self.db_path = db_path
        self.max_size = max_size
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS queue (
                    id TEXT PRIMARY KEY,
                    message TEXT NOT NULL,
                    priority INTEGER DEFAULT 2,
                    status TEXT DEFAULT 'pending',
                    submitted_at REAL,
                    started_at REAL,
                    completed_at REAL,
                    result TEXT,
                    error TEXT
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON queue(status, priority, submitted_at)")

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def submit(self, message: str, priority: int = 2) -> dict:
        """Submit work. Returns backpressure if queue is full."""
        with self._conn() as conn:
            pending_count = conn.execute(
                "SELECT COUNT(*) as cnt FROM queue WHERE status = 'pending'"
            ).fetchone()["cnt"]

            if pending_count >= self.max_size:
                return {
                    "status": "rejected",
                    "reason": "queue_full",
                    "queue_depth": pending_count,
                    "retry_after_seconds": 30
                }

            job_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO queue (id, message, priority, submitted_at) VALUES (?, ?, ?, ?)",
                (job_id, message, priority, time.time())
            )
            return {"status": "queued", "job_id": job_id, "queue_depth": pending_count + 1}

    def claim_next(self) -> dict | None:
        """Atomically claim the next pending job (highest priority first)."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, message FROM queue WHERE status='pending' ORDER BY priority, submitted_at LIMIT 1"
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE queue SET status='processing', started_at=? WHERE id=?",
                (time.time(), row["id"])
            )
            return {"id": row["id"], "message": row["message"]}

    def complete(self, job_id: str, result: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE queue SET status='done', completed_at=?, result=? WHERE id=?",
                (time.time(), result, job_id)
            )

    def fail(self, job_id: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE queue SET status='failed', error=? WHERE id=?",
                (error, job_id)
            )

    def stats(self) -> dict:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM queue GROUP BY status"
            ).fetchall()
        return {row["status"]: row["cnt"] for row in rows}


async def run_durable_worker(queue: DurableAgentQueue, worker_id: int):
    """Worker that processes jobs from the durable queue."""
    logger.info(f"Worker {worker_id} started")
    while True:
        job = queue.claim_next()
        if job is None:
            await asyncio.sleep(0.5)  # poll interval
            continue

        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": job["message"]}]
            )
            queue.complete(job["id"], response.content[0].text)
            logger.info(f"Worker {worker_id}: completed {job['id']}")
        except Exception as e:
            queue.fail(job["id"], str(e))
            logger.error(f"Worker {worker_id}: failed {job['id']}: {e}")
```

### Option 5: Queue depth monitoring and alerting

```python
import anthropic
import asyncio
import time
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)
client = anthropic.AsyncAnthropic()

@dataclass
class QueueHealthMetrics:
    queue_depth: int
    max_depth: int
    throughput_per_second: float
    avg_wait_seconds: float
    drop_rate: float

    @property
    def utilization(self) -> float:
        return self.queue_depth / max(1, self.max_depth)

    @property
    def estimated_drain_seconds(self) -> float:
        if self.throughput_per_second <= 0:
            return float("inf")
        return self.queue_depth / self.throughput_per_second

    @property
    def health(self) -> str:
        if self.utilization >= 0.9:
            return "critical"
        if self.utilization >= 0.7:
            return "degraded"
        if self.drop_rate > 0.05:
            return "shedding"
        return "healthy"


class MonitoredQueue:
    def __init__(self, max_size: int = 100, workers: int = 5):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_size)
        self._max_size = max_size
        self._workers_count = workers
        self._processed = 0
        self._dropped = 0
        self._wait_times: list[float] = []
        self._window_start = time.monotonic()
        self._window_processed = 0

    async def submit(self, message: str, priority: int = 2) -> dict:
        if self._queue.full():
            self._dropped += 1
            metrics = self.get_metrics()
            logger.warning(
                f"Queue full! health={metrics.health} "
                f"utilization={metrics.utilization:.0%} "
                f"drain_est={metrics.estimated_drain_seconds:.0f}s"
            )
            # Alert if this is sustained:
            if self._dropped > 10 and metrics.drop_rate > 0.1:
                await self._send_alert(metrics)
            return {
                "status": "rejected",
                "metrics": {
                    "health": metrics.health,
                    "utilization": metrics.utilization,
                    "retry_after": max(5, int(metrics.estimated_drain_seconds))
                }
            }

        submitted_at = time.monotonic()
        await self._queue.put((submitted_at, message))
        return {"status": "queued", "depth": self._queue.qsize()}

    async def _worker(self):
        while True:
            submitted_at, message = await self._queue.get()
            wait_time = time.monotonic() - submitted_at
            self._wait_times.append(wait_time)
            if len(self._wait_times) > 1000:
                self._wait_times = self._wait_times[-500:]

            try:
                await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{"role": "user", "content": message}]
                )
                self._processed += 1
                self._window_processed += 1
            finally:
                self._queue.task_done()

    def get_metrics(self) -> QueueHealthMetrics:
        now = time.monotonic()
        window_elapsed = now - self._window_start
        if window_elapsed >= 60:
            throughput = self._window_processed / window_elapsed
            self._window_start = now
            self._window_processed = 0
        else:
            throughput = self._window_processed / max(1, window_elapsed)

        total = self._processed + self._dropped
        drop_rate = self._dropped / max(1, total)
        avg_wait = sum(self._wait_times[-100:]) / max(1, len(self._wait_times[-100:]))

        return QueueHealthMetrics(
            queue_depth=self._queue.qsize(),
            max_depth=self._max_size,
            throughput_per_second=throughput,
            avg_wait_seconds=avg_wait,
            drop_rate=drop_rate
        )

    async def _send_alert(self, metrics: QueueHealthMetrics):
        """Send alert when queue health degrades."""
        logger.critical(
            f"QUEUE ALERT: health={metrics.health}, "
            f"utilization={metrics.utilization:.0%}, "
            f"drop_rate={metrics.drop_rate:.1%}, "
            f"avg_wait={metrics.avg_wait_seconds:.1f}s"
        )
        # In production: send to PagerDuty, Slack, etc.
```

### Option 6: Backpressure propagation to HTTP clients

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

# Translate internal queue state into proper HTTP responses
# so callers can implement exponential backoff.

@dataclass
class BackpressureResponse:
    should_reject: bool
    http_status: int          # 200, 429, or 503
    retry_after_seconds: int
    reason: str

    def to_headers(self) -> dict:
        headers = {}
        if self.retry_after_seconds > 0:
            headers["Retry-After"] = str(self.retry_after_seconds)
        if self.http_status == 429:
            headers["X-RateLimit-Reset"] = str(int(time.time() + self.retry_after_seconds))
        return headers


class BackpressurePolicy:
    """
    Translates queue state into HTTP backpressure signals.
    429 = rate limited (client-side limit, retry soon)
    503 = overloaded (system-side, retry later)
    """
    def __init__(self, queue_max: int = 100):
        self.queue_max = queue_max

    def evaluate(self, queue_depth: int, rate_limited: bool = False) -> BackpressureResponse:
        utilization = queue_depth / self.queue_max

        if rate_limited:
            return BackpressureResponse(
                should_reject=True,
                http_status=429,
                retry_after_seconds=5,
                reason="rate_limit_exceeded"
            )

        if utilization >= 0.95:
            return BackpressureResponse(
                should_reject=True,
                http_status=503,
                retry_after_seconds=30,
                reason="service_overloaded"
            )

        if utilization >= 0.80:
            return BackpressureResponse(
                should_reject=True,
                http_status=503,
                retry_after_seconds=10,
                reason="queue_near_capacity"
            )

        return BackpressureResponse(
            should_reject=False,
            http_status=200,
            retry_after_seconds=0,
            reason="ok"
        )


# FastAPI usage:
# policy = BackpressurePolicy(queue_max=100)
#
# @app.post("/process")
# async def process(body: RequestBody):
#     bp = policy.evaluate(queue.qsize())
#     if bp.should_reject:
#         raise HTTPException(
#             status_code=bp.http_status,
#             detail={"reason": bp.reason, "retry_after": bp.retry_after_seconds},
#             headers=bp.to_headers()
#         )
#     return await queue.submit(body.message)
```

## Overflow Strategy Comparison

| Strategy | Behavior on Full Queue | When to Use |
|---|---|---|
| Drop silently | Discard request, no signal | Never (data loss) |
| Raise exception | 500 error, no guidance | Development only |
| Return 429 + Retry-After | Client retries intelligently | Rate limiting |
| Return 503 + Retry-After | Client retries, system recovers | Temporary overload |
| Priority shedding | Drop low-priority, protect critical | Heterogeneous load |
| Persistent queue | No drop, durable backlog | Async/batch workloads |

## Expected Token Savings
No direct token savings — but preventing silent drops means every submitted request is eventually processed, maximizing the value of tokens already spent on API calls. Queue health monitoring (Option 5) also provides early warning before overload causes cascading failures that waste far more resources.

## Environment
- Any agent deployed as a web service under variable load; critical for agents handling bursty traffic or downstream rate limits; the bounded asyncio queue (Option 1) is the baseline for any async agent; use priority queues (Option 2) when request types have different business value; use the durable queue (Option 4) for batch processing or when jobs must survive restarts; always emit Retry-After headers (Option 6) — clients that receive 503 without guidance will hammer the service
