---
layout: solution
title: "Agent Doesn't Implement Request Queuing with Overflow Handling"
category: reliability
description: "Agents that process requests as fast as they arrive can overload downstream APIs, exhaust rate limits, and crash under burst traffic. A request queue with overflow handling absorbs bursts, enforces backpressure, and gracefully sheds load when capacity is exceeded."
tags: [reliability, queuing, backpressure, rate-limiting, overflow, python]
---

## Problem

When an agent receives a burst of requests simultaneously, several failure modes emerge: simultaneous API calls exceed rate limits, memory spikes from unbounded queues crash the process, and slow consumers block all others. Request queuing decouples ingestion from processing, while overflow policies (reject, drop-oldest, shed) protect against unbounded growth.

## Solutions

### Option 1: Bounded Async Queue with Rejection Policy

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

class OverflowPolicy(Enum):
    REJECT = "reject"       # Reject new requests when full
    DROP_OLDEST = "drop_oldest"  # Drop oldest queued item to make room
    DROP_NEWEST = "drop_newest"  # Drop the incoming request silently

@dataclass
class QueuedRequest:
    request_id: str
    prompt: str
    queued_at: float = field(default_factory=time.monotonic)
    max_wait_seconds: float = 30.0

    @property
    def is_expired(self) -> bool:
        return time.monotonic() - self.queued_at > self.max_wait_seconds

@dataclass
class QueueStats:
    total_enqueued: int = 0
    total_processed: int = 0
    total_rejected: int = 0
    total_dropped: int = 0
    total_expired: int = 0

class BoundedRequestQueue:
    def __init__(self, max_size: int = 20, workers: int = 3,
                 policy: OverflowPolicy = OverflowPolicy.REJECT):
        self._queue: asyncio.Queue[QueuedRequest] = asyncio.Queue(maxsize=max_size)
        self._policy = policy
        self._workers = workers
        self._stats = QueueStats()
        self._client = anthropic.AsyncAnthropic()

    async def enqueue(self, request: QueuedRequest) -> bool:
        """Returns True if accepted, False if rejected/dropped."""
        if not self._queue.full():
            await self._queue.put(request)
            self._stats.total_enqueued += 1
            return True

        if self._policy == OverflowPolicy.REJECT:
            self._stats.total_rejected += 1
            print(f"[QUEUE FULL] Rejected {request.request_id}")
            return False

        if self._policy == OverflowPolicy.DROP_OLDEST:
            try:
                dropped = self._queue.get_nowait()
                self._stats.total_dropped += 1
                print(f"[DROP OLDEST] Dropped {dropped.request_id}")
            except asyncio.QueueEmpty:
                pass
            await self._queue.put(request)
            self._stats.total_enqueued += 1
            return True

        # DROP_NEWEST: silently drop incoming
        self._stats.total_dropped += 1
        print(f"[DROP NEWEST] Dropped {request.request_id}")
        return False

    async def _worker(self, worker_id: int) -> None:
        while True:
            try:
                request = await asyncio.wait_for(self._queue.get(), timeout=3.0)
            except asyncio.TimeoutError:
                return

            if request.is_expired:
                self._stats.total_expired += 1
                print(f"[EXPIRED] {request.request_id} waited too long")
                self._queue.task_done()
                continue

            try:
                wait_ms = (time.monotonic() - request.queued_at) * 1000
                response = await self._client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=60,
                    messages=[{"role": "user", "content": request.prompt}],
                )
                self._stats.total_processed += 1
                print(f"[W{worker_id}] {request.request_id} "
                      f"(waited {wait_ms:.0f}ms): {response.content[0].text[:50]}")
            except Exception as e:
                print(f"[W{worker_id}] {request.request_id} failed: {e}")
            finally:
                self._queue.task_done()

    async def run(self, requests: list[QueuedRequest]) -> None:
        worker_tasks = [asyncio.create_task(self._worker(i))
                        for i in range(self._workers)]

        # Enqueue all (simulating burst)
        for req in requests:
            await self.enqueue(req)
            await asyncio.sleep(0.05)  # small inter-arrival gap

        await self._queue.join()
        for t in worker_tasks:
            t.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)

        print(f"\nQueue stats: {self._stats}")

async def main():
    import uuid
    queue = BoundedRequestQueue(max_size=5, workers=2,
                                policy=OverflowPolicy.DROP_OLDEST)
    requests = [
        QueuedRequest(str(uuid.uuid4())[:8], f"What is {i}+{i}?", max_wait_seconds=10.0)
        for i in range(12)  # 12 requests, queue size 5 — overflow will occur
    ]
    await queue.run(requests)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Prevents retry storms by enforcing orderly processing
# Environment: pip install anthropic
```

### Option 2: Priority Queue with Fair Scheduling

```python
import anthropic
import asyncio
import heapq
import time
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

class Priority(IntEnum):
    CRITICAL = 0   # Highest priority (lowest number = served first)
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BATCH = 4      # Lowest priority

@dataclass(order=True)
class PriorityRequest:
    priority: Priority
    queued_at: float = field(compare=True, default_factory=time.monotonic)
    request_id: str = field(compare=False, default_factory=lambda: str(uuid.uuid4())[:8])
    prompt: str = field(compare=False, default="")
    tenant_id: str = field(compare=False, default="default")

class PriorityRequestQueue:
    def __init__(self, max_size: int = 50, workers: int = 3):
        self._heap: list[PriorityRequest] = []
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Event()
        self._max_size = max_size
        self._workers = workers
        self._stats: dict[str, int] = {"processed": 0, "rejected": 0}

    async def enqueue(self, request: PriorityRequest) -> bool:
        async with self._lock:
            if len(self._heap) >= self._max_size:
                self._stats["rejected"] += 1
                return False
            heapq.heappush(self._heap, request)
            self._not_empty.set()
            return True

    async def dequeue(self) -> Optional[PriorityRequest]:
        while True:
            async with self._lock:
                if self._heap:
                    item = heapq.heappop(self._heap)
                    if not self._heap:
                        self._not_empty.clear()
                    return item
            try:
                await asyncio.wait_for(self._not_empty.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                return None  # No more work

    async def _worker(self, worker_id: int, client: anthropic.AsyncAnthropic) -> None:
        while True:
            request = await self.dequeue()
            if request is None:
                return

            wait_ms = (time.monotonic() - request.queued_at) * 1000
            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=40,
                    messages=[{"role": "user", "content": request.prompt}],
                )
                self._stats["processed"] += 1
                print(f"[W{worker_id}|{request.priority.name:8}] "
                      f"{request.request_id} (wait={wait_ms:.0f}ms) "
                      f"tenant={request.tenant_id}: {response.content[0].text[:40]}")
            except Exception as e:
                print(f"[W{worker_id}] {request.request_id} error: {e}")

    async def run_until_empty(self, client: anthropic.AsyncAnthropic) -> None:
        workers = [asyncio.create_task(self._worker(i, client))
                   for i in range(self._workers)]
        await asyncio.gather(*workers)
        print(f"\nPriority queue stats: {self._stats}")

async def main():
    client = anthropic.AsyncAnthropic()
    pqueue = PriorityRequestQueue(max_size=30, workers=2)

    # Enqueue mixed-priority requests
    requests = [
        PriorityRequest(Priority.BATCH,    prompt="List 5 hobbies.",       tenant_id="free"),
        PriorityRequest(Priority.NORMAL,   prompt="What is Python?",       tenant_id="pro"),
        PriorityRequest(Priority.CRITICAL, prompt="System health check?",  tenant_id="ops"),
        PriorityRequest(Priority.BATCH,    prompt="Name 3 animals.",       tenant_id="free"),
        PriorityRequest(Priority.HIGH,     prompt="Summarize AI briefly.", tenant_id="pro"),
        PriorityRequest(Priority.NORMAL,   prompt="What is 7×8?",          tenant_id="pro"),
        PriorityRequest(Priority.CRITICAL, prompt="Alert: service down.",  tenant_id="ops"),
        PriorityRequest(Priority.LOW,      prompt="Tell me a fun fact.",   tenant_id="free"),
    ]

    for req in requests:
        accepted = await pqueue.enqueue(req)
        print(f"[ENQUEUE] {req.priority.name:8} {req.request_id} "
              f"{'accepted' if accepted else 'rejected'}")

    await pqueue.run_until_empty(client)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Priority queuing ensures critical requests aren't starved
# Environment: pip install anthropic
```

### Option 3: Token Bucket Queue with Rate-Limited Workers

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

@dataclass
class TokenBucket:
    """Leaky token bucket for rate limiting workers."""
    capacity: float       # Max tokens (= max burst)
    refill_rate: float    # Tokens added per second
    _tokens: float = field(init=False)
    _last_refill: float = field(init=False, default_factory=time.monotonic)
    _lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)

    def __post_init__(self):
        self._tokens = self.capacity

    async def acquire(self, tokens: float = 1.0) -> float:
        """Returns wait time in seconds before token was acquired."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_refill
            self._tokens = min(self.capacity, self._tokens + elapsed * self.refill_rate)
            self._last_refill = now

            if self._tokens >= tokens:
                self._tokens -= tokens
                return 0.0

            # Calculate wait time
            deficit = tokens - self._tokens
            wait = deficit / self.refill_rate
            return wait

class RateLimitedQueue:
    def __init__(self, max_size: int = 30, requests_per_second: float = 2.0,
                 burst_size: float = 5.0):
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=max_size)
        self._bucket = TokenBucket(capacity=burst_size, refill_rate=requests_per_second)
        self._processed = 0
        self._rate_waits: list[float] = []

    async def enqueue(self, item: dict) -> bool:
        try:
            self._queue.put_nowait(item)
            return True
        except asyncio.QueueFull:
            print(f"[OVERFLOW] Queue full, rejected: {item['id']}")
            return False

    async def process_one(self, client: anthropic.AsyncAnthropic) -> bool:
        try:
            item = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return False

        # Wait for rate limit token
        wait = await self._bucket.acquire(tokens=1.0)
        if wait > 0:
            self._rate_waits.append(wait)
            print(f"[RATE LIMIT] Waiting {wait:.2f}s before processing {item['id']}")
            await asyncio.sleep(wait)

        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=40,
                messages=[{"role": "user", "content": item["prompt"]}],
            )
            self._processed += 1
            print(f"[OK] {item['id']}: {response.content[0].text[:50]}")
            return True
        except Exception as e:
            print(f"[FAIL] {item['id']}: {e}")
            return True
        finally:
            self._queue.task_done()

    async def drain(self, client: anthropic.AsyncAnthropic) -> None:
        while not self._queue.empty():
            await self.process_one(client)
        avg_wait = sum(self._rate_waits) / len(self._rate_waits) if self._rate_waits else 0
        print(f"\nProcessed: {self._processed} | "
              f"Rate-limit waits: {len(self._rate_waits)} | "
              f"Avg wait: {avg_wait:.2f}s")

async def main():
    client = anthropic.AsyncAnthropic()
    queue = RateLimitedQueue(max_size=10, requests_per_second=3.0, burst_size=3.0)

    items = [{"id": f"req-{i:02d}", "prompt": f"What is {i}×{i}?"} for i in range(8)]
    for item in items:
        queue.enqueue(item)

    await queue.drain(client)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Rate limiting prevents 429 errors that waste retry tokens
# Environment: pip install anthropic
```

### Option 4: Queue with Timeout and SLA Tracking

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from statistics import mean, median

@dataclass
class SLARequest:
    request_id: str
    prompt: str
    sla_ms: float          # Target response time in milliseconds
    queued_at: float = field(default_factory=time.monotonic)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    sla_met: Optional[bool] = None

    @property
    def queue_wait_ms(self) -> float:
        if self.started_at is None:
            return (time.monotonic() - self.queued_at) * 1000
        return (self.started_at - self.queued_at) * 1000

    @property
    def total_latency_ms(self) -> Optional[float]:
        if self.completed_at is None:
            return None
        return (self.completed_at - self.queued_at) * 1000

from typing import Optional

class SLATrackingQueue:
    def __init__(self, max_size: int = 20, workers: int = 3):
        self._queue: asyncio.Queue[SLARequest] = asyncio.Queue(maxsize=max_size)
        self._completed: list[SLARequest] = []
        self._lock = asyncio.Lock()

    async def enqueue(self, request: SLARequest) -> bool:
        try:
            self._queue.put_nowait(request)
            return True
        except asyncio.QueueFull:
            return False

    async def _worker(self, worker_id: int, client: anthropic.AsyncAnthropic) -> None:
        while True:
            try:
                req = await asyncio.wait_for(self._queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                return

            req.started_at = time.monotonic()
            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=50,
                    messages=[{"role": "user", "content": req.prompt}],
                )
                req.completed_at = time.monotonic()
                latency = req.total_latency_ms or 0
                req.sla_met = latency <= req.sla_ms
                sla_icon = "✓" if req.sla_met else "✗"
                print(f"[W{worker_id}] {sla_icon} {req.request_id} "
                      f"latency={latency:.0f}ms SLA={req.sla_ms:.0f}ms "
                      f"| {response.content[0].text[:40]}")
            except Exception as e:
                req.completed_at = time.monotonic()
                req.sla_met = False
                print(f"[W{worker_id}] ERROR {req.request_id}: {e}")
            finally:
                async with self._lock:
                    self._completed.append(req)
                self._queue.task_done()

    async def run(self, requests: list[SLARequest],
                  client: anthropic.AsyncAnthropic) -> dict:
        workers = [asyncio.create_task(self._worker(i, client))
                   for i in range(3)]
        for req in requests:
            self.enqueue(req)
        await self._queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        completed = self._completed
        latencies = [r.total_latency_ms for r in completed if r.total_latency_ms]
        sla_met = [r for r in completed if r.sla_met]
        return {
            "total": len(completed),
            "sla_met": len(sla_met),
            "sla_rate_pct": len(sla_met) / max(len(completed), 1) * 100,
            "p50_ms": median(latencies) if latencies else 0,
            "p99_ms": sorted(latencies)[int(len(latencies) * 0.99)] if latencies else 0,
            "mean_ms": mean(latencies) if latencies else 0,
        }

async def main():
    client = anthropic.AsyncAnthropic()
    queue = SLATrackingQueue(max_size=15, workers=3)

    requests = [
        SLARequest(f"req-{i:02d}", f"What is {i}+{i}?", sla_ms=5000.0)
        for i in range(6)
    ]
    report = await queue.run(requests, client)
    print(f"\nSLA Report: {report}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — SLA tracking identifies slow paths for optimization
# Environment: pip install anthropic
```

### Option 5: Multi-Tier Queue with Load Shedding

```python
import anthropic
import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

class Tier(Enum):
    FAST = "fast"     # Small, quick requests (< 200 tokens)
    SLOW = "slow"     # Large, complex requests
    BULK = "bulk"     # Batch/offline jobs

TIER_WORKERS = {Tier.FAST: 4, Tier.SLOW: 2, Tier.BULK: 1}
TIER_LIMITS = {Tier.FAST: 10, Tier.SLOW: 5, Tier.BULK: 20}

@dataclass
class TieredRequest:
    request_id: str
    prompt: str
    tier: Tier
    max_tokens: int
    queued_at: float = field(default_factory=time.monotonic)

class MultiTierQueue:
    def __init__(self):
        self._queues = {
            tier: asyncio.Queue(maxsize=limit)
            for tier, limit in TIER_LIMITS.items()
        }
        self._shed_count = 0
        self._processed = {t: 0 for t in Tier}

    def classify(self, prompt: str, max_tokens: int) -> Tier:
        if max_tokens <= 100 and len(prompt) < 200:
            return Tier.FAST
        if max_tokens <= 500:
            return Tier.SLOW
        return Tier.BULK

    async def enqueue(self, request: TieredRequest) -> bool:
        queue = self._queues[request.tier]
        if queue.full():
            # Load shedding: try downgrading to bulk tier
            if request.tier != Tier.BULK and not self._queues[Tier.BULK].full():
                print(f"[SHED→BULK] {request.request_id} downgraded to BULK")
                request.tier = Tier.BULK
                await self._queues[Tier.BULK].put(request)
                return True
            self._shed_count += 1
            print(f"[SHED] {request.request_id} dropped (all queues full)")
            return False
        await queue.put(request)
        return True

    async def _worker(self, tier: Tier, worker_id: int,
                      client: anthropic.AsyncAnthropic) -> None:
        queue = self._queues[tier]
        while True:
            try:
                req = await asyncio.wait_for(queue.get(), timeout=2.0)
            except asyncio.TimeoutError:
                return
            wait_ms = (time.monotonic() - req.queued_at) * 1000
            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=req.max_tokens,
                    messages=[{"role": "user", "content": req.prompt}],
                )
                self._processed[tier] += 1
                print(f"[{tier.value.upper()}:W{worker_id}] "
                      f"{req.request_id} wait={wait_ms:.0f}ms: "
                      f"{response.content[0].text[:45]}")
            except Exception as e:
                print(f"[{tier.value.upper()}:W{worker_id}] ERROR: {e}")
            finally:
                queue.task_done()

    async def run(self, requests: list[TieredRequest],
                  client: anthropic.AsyncAnthropic) -> None:
        workers = []
        for tier, n in TIER_WORKERS.items():
            for i in range(n):
                workers.append(asyncio.create_task(self._worker(tier, i, client)))

        for req in requests:
            await self.enqueue(req)

        for q in self._queues.values():
            await q.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        print(f"\nProcessed by tier: {self._processed} | Shed: {self._shed_count}")

async def main():
    client = anthropic.AsyncAnthropic()
    mtq = MultiTierQueue()

    requests = [
        TieredRequest(f"req-{i:02d}", prompt, Tier.FAST, 50)
        for i, prompt in enumerate([
            "Yes or no: is water wet?", "What color is the sky?",
            "Name one fruit.", "What is 9+1?",
            "Is Python a programming language?", "Name one planet.",
        ])
    ]

    await mtq.run(requests, client)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Tier routing prevents cheap fast requests from waiting behind slow ones
# Environment: pip install anthropic
```

### Option 6: Persistent Queue with Crash Recovery

```python
import anthropic
import asyncio
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Generator

class RequestState(Enum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class PersistedRequest:
    request_id: str
    prompt: str
    max_tokens: int
    state: RequestState = RequestState.PENDING
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    result: Optional[str] = None
    error: Optional[str] = None
    attempts: int = 0

class PersistentQueue:
    def __init__(self, db_path: str = "/tmp/agent_queue.db",
                 workers: int = 2, max_attempts: int = 3):
        self.db_path = db_path
        self.workers = workers
        self.max_attempts = max_attempts
        self._init_db()
        self._recover_in_flight()

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS request_queue (
                    request_id TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    max_tokens INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    created_at REAL NOT NULL,
                    started_at REAL,
                    completed_at REAL,
                    result TEXT,
                    error TEXT,
                    attempts INTEGER DEFAULT 0
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_state ON request_queue(state)")

    def _recover_in_flight(self) -> int:
        """Reset in-flight requests to pending on startup (crash recovery)."""
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE request_queue SET state='pending' WHERE state='in_flight'"
            )
            if cur.rowcount:
                print(f"[RECOVERY] Reset {cur.rowcount} in-flight requests to pending")
            return cur.rowcount

    def enqueue(self, request: PersistedRequest) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO request_queue
                   (request_id, prompt, max_tokens, state, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (request.request_id, request.prompt, request.max_tokens,
                 RequestState.PENDING.value, request.created_at)
            )

    def _claim_next(self) -> Optional[PersistedRequest]:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM request_queue
                   WHERE state='pending' AND attempts < ?
                   ORDER BY created_at ASC LIMIT 1""",
                (self.max_attempts,)
            ).fetchone()
            if not row:
                return None
            conn.execute(
                """UPDATE request_queue
                   SET state='in_flight', started_at=?, attempts=attempts+1
                   WHERE request_id=?""",
                (time.time(), row["request_id"])
            )
            return PersistedRequest(
                request_id=row["request_id"], prompt=row["prompt"],
                max_tokens=row["max_tokens"], state=RequestState.IN_FLIGHT,
                created_at=row["created_at"], attempts=row["attempts"] + 1,
            )

    def _complete(self, request_id: str, result: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE request_queue
                   SET state='completed', completed_at=?, result=?
                   WHERE request_id=?""",
                (time.time(), result, request_id)
            )

    def _fail(self, request_id: str, error: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """UPDATE request_queue
                   SET state='failed', error=?, completed_at=?
                   WHERE request_id=?""",
                (error, time.time(), request_id)
            )

    async def _worker(self, worker_id: int, client: anthropic.AsyncAnthropic) -> None:
        while True:
            request = self._claim_next()
            if request is None:
                await asyncio.sleep(0.5)
                continue

            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=request.max_tokens,
                    messages=[{"role": "user", "content": request.prompt}],
                )
                result = response.content[0].text
                self._complete(request.request_id, result)
                print(f"[W{worker_id}] {request.request_id[:8]} "
                      f"(attempt {request.attempts}): {result[:50]}")
            except Exception as e:
                self._fail(request.request_id, str(e))
                print(f"[W{worker_id}] {request.request_id[:8]} failed: {e}")

    async def run(self, client: anthropic.AsyncAnthropic,
                  duration_seconds: float = 10.0) -> dict:
        workers = [asyncio.create_task(self._worker(i, client))
                   for i in range(self.workers)]
        await asyncio.sleep(duration_seconds)
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        with self._conn() as conn:
            stats = {
                row["state"]: row["count"]
                for row in conn.execute(
                    "SELECT state, COUNT(*) as count FROM request_queue GROUP BY state"
                ).fetchall()
            }
        return stats

async def main():
    client = anthropic.AsyncAnthropic()
    pqueue = PersistentQueue(workers=2, max_attempts=2)

    for i in range(5):
        pqueue.enqueue(PersistedRequest(
            request_id=str(uuid.uuid4()),
            prompt=f"What is the square of {i+2}?",
            max_tokens=30,
        ))

    stats = await pqueue.run(client, duration_seconds=15.0)
    print(f"\nFinal queue stats: {stats}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: N/A — persistence prevents work loss on crash
# Environment: pip install anthropic; sqlite3 is stdlib
```

## Comparison

| Option | Queue Type | Overflow Policy | Persistence | Best For |
|--------|-----------|-----------------|-------------|----------|
| 1. Bounded + Rejection | FIFO | Reject/Drop | None | Simple burst protection |
| 2. Priority | Min-heap | Reject | None | Multi-priority workloads |
| 3. Token Bucket | FIFO | Reject | None | Rate limit compliance |
| 4. SLA Tracking | FIFO | Reject | None | Latency-sensitive APIs |
| 5. Multi-Tier | 3×FIFO | Shed/Downgrade | None | Mixed request types |
| 6. Persistent | FIFO SQLite | Reject | SQLite | Crash-safe production |
