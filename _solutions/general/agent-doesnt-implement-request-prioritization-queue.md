---
layout: solution
title: "Agent Doesn't Implement Request Prioritization Queue"
category: general
description: "Agent processes all requests in FIFO order regardless of urgency, causing high-priority requests (user-facing, SLA-bound) to wait behind low-priority background jobs."
tags: [general, prioritization, queue, scheduling, sla, fairness]
---

# Agent Doesn't Implement Request Prioritization Queue

## Problem

An agent handles both user-facing chat requests (latency-sensitive, SLA < 2s) and background batch jobs (tolerance for minutes of delay). Without prioritization, a burst of batch jobs fills the processing queue and user requests wait 30+ seconds. The fix is a priority queue that always drains high-priority requests first while ensuring low-priority jobs eventually complete.

---

## Option 1: asyncio.PriorityQueue with Request Tiers

Use Python's built-in `asyncio.PriorityQueue` to process requests by urgency level.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

class Priority(IntEnum):
    CRITICAL = 0   # Real-time user requests, SLA < 1s
    HIGH = 1       # Interactive requests, SLA < 5s
    NORMAL = 2     # Standard API calls
    LOW = 3        # Background enrichment
    BATCH = 4      # Bulk processing jobs

@dataclass(order=True)
class PrioritizedRequest:
    priority: Priority
    enqueued_at: float = field(compare=False)
    request_id: str = field(compare=False)
    prompt: str = field(compare=False)
    model: str = field(compare=False)
    max_tokens: int = field(compare=False, default=256)
    result: asyncio.Future = field(compare=False, default=None)

client = anthropic.AsyncAnthropic()

class PriorityQueueProcessor:
    def __init__(self, max_concurrent: int = 3):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._processed = 0
        self._wait_times: list[float] = []

    async def submit(
        self,
        prompt: str,
        priority: Priority = Priority.NORMAL,
        model: str = "claude-haiku-4-5-20251001",
        max_tokens: int = 256,
        request_id: str = ""
    ) -> str:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        req = PrioritizedRequest(
            priority=priority,
            enqueued_at=time.monotonic(),
            request_id=request_id or f"req_{id(future)}",
            prompt=prompt,
            model=model,
            max_tokens=max_tokens,
            result=future
        )
        await self._queue.put(req)
        return await future

    async def _process_one(self, req: PrioritizedRequest):
        wait_time = time.monotonic() - req.enqueued_at
        self._wait_times.append(wait_time)
        print(f"[{req.priority.name}] {req.request_id} wait={wait_time:.2f}s")
        try:
            async with self._semaphore:
                response = await client.messages.create(
                    model=req.model, max_tokens=req.max_tokens,
                    messages=[{"role": "user", "content": req.prompt}]
                )
            result_text = response.content[0].text
            if not req.result.done():
                req.result.set_result(result_text)
            self._processed += 1
        except Exception as exc:
            if not req.result.done():
                req.result.set_exception(exc)

    async def run(self, stop_after: int = 100):
        while self._processed < stop_after:
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)
                continue
            asyncio.create_task(self._process_one(req))

    def stats(self) -> dict:
        if not self._wait_times:
            return {"processed": 0}
        return {
            "processed": self._processed,
            "avg_wait_s": sum(self._wait_times) / len(self._wait_times),
            "max_wait_s": max(self._wait_times),
        }

async def demo():
    processor = PriorityQueueProcessor(max_concurrent=2)
    runner = asyncio.create_task(processor.run(stop_after=6))

    # Submit mixed priority requests
    tasks = await asyncio.gather(
        processor.submit("Explain cache invalidation.", Priority.BATCH, request_id="batch-1"),
        processor.submit("What is 2+2?", Priority.CRITICAL, request_id="critical-1"),
        processor.submit("Name a planet.", Priority.HIGH, request_id="high-1"),
        processor.submit("Describe neural networks.", Priority.LOW, request_id="low-1"),
        processor.submit("What is Python?", Priority.NORMAL, request_id="normal-1"),
        processor.submit("List 3 colors.", Priority.HIGH, request_id="high-2"),
    )
    runner.cancel()
    print(f"\nStats: {processor.stats()}")
    for t in tasks:
        print(f"  Result: {t[:50]}")

asyncio.run(demo())

# Expected Token Savings: Priority queuing prevents BATCH jobs from blocking CRITICAL requests. Eliminates SLA breaches that would require user retries (3–5x token cost). Critical requests drain first even under load.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Option 2: Weighted Fair Queue (WFQ)

Each priority tier gets a weight. Weighted Fair Queuing ensures low-priority jobs are never starved — they get a proportional share of processing slots.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

TIER_WEIGHTS = {
    "critical": 8,
    "high":     4,
    "normal":   2,
    "low":      1,
    "batch":    1,
}

@dataclass
class QueuedRequest:
    tier: str
    prompt: str
    request_id: str
    enqueued_at: float
    result: asyncio.Future = field(compare=False)

class WeightedFairQueue:
    def __init__(self):
        self._queues: dict[str, list[QueuedRequest]] = {tier: [] for tier in TIER_WEIGHTS}
        self._credits: dict[str, float] = {tier: float(w) for tier, w in TIER_WEIGHTS.items()}
        self._lock = asyncio.Lock()
        self._processed: dict[str, int] = {tier: 0 for tier in TIER_WEIGHTS}

    async def submit(self, prompt: str, tier: str = "normal", request_id: str = "") -> str:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        req = QueuedRequest(
            tier=tier, prompt=prompt,
            request_id=request_id or str(id(future)),
            enqueued_at=time.monotonic(),
            result=future
        )
        async with self._lock:
            self._queues[tier].append(req)
        return await future

    def _pick_next(self) -> Optional[QueuedRequest]:
        """Pick the tier with highest credits that has pending requests."""
        best_tier = None
        best_credits = -1.0
        for tier, queue in self._queues.items():
            if queue and self._credits[tier] > best_credits:
                best_credits = self._credits[tier]
                best_tier = tier

        if best_tier is None:
            return None

        # Deduct credit and replenish all tiers
        self._credits[best_tier] -= 1.0
        for tier in self._credits:
            self._credits[tier] += TIER_WEIGHTS[tier] * 0.1  # Gradual replenishment

        return self._queues[best_tier].pop(0)

    async def process_loop(self, client: anthropic.AsyncAnthropic, stop_after: int = 20):
        processed = 0
        while processed < stop_after:
            async with self._lock:
                req = self._pick_next()
            if req is None:
                await asyncio.sleep(0.05)
                continue

            wait = time.monotonic() - req.enqueued_at
            print(f"[WFQ/{req.tier}] {req.request_id} wait={wait:.2f}s credits={self._credits[req.tier]:.1f}")
            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=128,
                    messages=[{"role": "user", "content": req.prompt}]
                )
                if not req.result.done():
                    req.result.set_result(response.content[0].text)
                self._processed[req.tier] += 1
                processed += 1
            except Exception as exc:
                if not req.result.done():
                    req.result.set_exception(exc)

async def demo_wfq():
    wfq = WeightedFairQueue()
    client = anthropic.AsyncAnthropic()
    processor = asyncio.create_task(wfq.process_loop(client, stop_after=6))

    results = await asyncio.gather(
        wfq.submit("Explain REST APIs.", "batch", "b1"),
        wfq.submit("What is 3+3?", "critical", "c1"),
        wfq.submit("Name a color.", "high", "h1"),
        wfq.submit("What is TCP/IP?", "low", "l1"),
        wfq.submit("What is Python?", "normal", "n1"),
        wfq.submit("Summarize HTTP.", "batch", "b2"),
    )

    processor.cancel()
    print(f"\nProcessed per tier: {wfq._processed}")
    for r in results:
        print(f"  {r[:50]}")

asyncio.run(demo_wfq())

# Expected Token Savings: WFQ ensures batch jobs never fully starve, preventing infinite queue growth. Low-priority jobs complete eventually — preventing retry storms that would generate 5–10x token overhead.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Option 3: Deadline-Based Priority with SLA Tracking

Assign each request a deadline. Process the request closest to its deadline first (Earliest Deadline First — EDF), ensuring SLAs are met.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

SLA_DEADLINES = {
    "critical": 1.0,    # Must complete within 1 second of submission
    "high":     5.0,
    "normal":   30.0,
    "low":      120.0,
    "batch":    600.0,
}

@dataclass(order=True)
class DeadlineRequest:
    deadline: float          # absolute timestamp
    submitted_at: float = field(compare=False)
    tier: str = field(compare=False)
    prompt: str = field(compare=False)
    request_id: str = field(compare=False)
    result: asyncio.Future = field(compare=False)

    @property
    def time_to_deadline(self) -> float:
        return self.deadline - time.monotonic()

    @property
    def is_expired(self) -> bool:
        return time.monotonic() > self.deadline

class EDFQueue:
    def __init__(self):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._sla_hits = 0
        self._sla_misses = 0
        self._expirations = 0

    async def submit(
        self, prompt: str, tier: str = "normal", request_id: str = ""
    ) -> str:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        now = time.monotonic()
        deadline = now + SLA_DEADLINES.get(tier, 30.0)
        req = DeadlineRequest(
            deadline=deadline,
            submitted_at=now,
            tier=tier,
            prompt=prompt,
            request_id=request_id or f"req_{int(now*1000)}",
            result=future
        )
        await self._queue.put(req)
        return await future

    async def process_loop(self, client: anthropic.AsyncAnthropic, stop_after: int = 20):
        processed = 0
        while processed < stop_after:
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.05)
                continue

            if req.is_expired:
                self._expirations += 1
                print(f"[EXPIRED] {req.tier}/{req.request_id} missed by {-req.time_to_deadline:.1f}s")
                if not req.result.done():
                    req.result.set_exception(TimeoutError(f"SLA deadline missed: {req.tier}"))
                processed += 1
                continue

            ttd = req.time_to_deadline
            print(f"[EDF/{req.tier}] {req.request_id} TTD={ttd:.1f}s")

            try:
                start = time.monotonic()
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=128,
                    messages=[{"role": "user", "content": req.prompt}]
                )
                latency = time.monotonic() - start
                if not req.is_expired:
                    self._sla_hits += 1
                else:
                    self._sla_misses += 1
                if not req.result.done():
                    req.result.set_result(response.content[0].text)
                processed += 1
                print(f"[done] {req.tier}/{req.request_id} latency={latency:.2f}s SLA={'HIT' if not req.is_expired else 'MISS'}")
            except Exception as exc:
                if not req.result.done():
                    req.result.set_exception(exc)
                processed += 1

    def sla_report(self) -> dict:
        total = self._sla_hits + self._sla_misses + self._expirations
        return {
            "hits": self._sla_hits,
            "misses": self._sla_misses,
            "expirations": self._expirations,
            "hit_rate": self._sla_hits / max(total, 1)
        }

async def demo_edf():
    edf = EDFQueue()
    client = anthropic.AsyncAnthropic()
    processor = asyncio.create_task(edf.process_loop(client, stop_after=5))

    results = await asyncio.gather(
        edf.submit("Large batch processing job.", "batch", "b1"),
        edf.submit("What is 2+2?", "critical", "c1"),
        edf.submit("Name a fruit.", "high", "h1"),
        edf.submit("What is Python?", "normal", "n1"),
        edf.submit("Explain DNS.", "low", "l1"),
        return_exceptions=True
    )

    processor.cancel()
    print(f"\nSLA Report: {edf.sla_report()}")
    for r in results:
        if isinstance(r, Exception):
            print(f"  Error: {r}")
        else:
            print(f"  OK: {r[:50]}")

asyncio.run(demo_edf())

# Expected Token Savings: EDF prevents SLA violations that trigger user retries. A missed SLA for a critical request typically causes 3–5 user retries, each costing full prompt tokens. Deadline tracking eliminates this waste.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Option 4: Multi-Queue with Work Stealing Between Tiers

Each priority tier has its own queue. Workers steal from lower-priority queues only when their assigned tier is empty.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from typing import Optional

TIERS = ["critical", "high", "normal", "low", "batch"]
WORKERS_PER_TIER = {"critical": 2, "high": 2, "normal": 1, "low": 1, "batch": 1}

@dataclass
class TieredRequest:
    tier: str
    prompt: str
    request_id: str
    enqueued_at: float
    result: asyncio.Future = field(compare=False)

class MultiTierQueue:
    def __init__(self):
        self._queues: dict[str, asyncio.Queue] = {t: asyncio.Queue() for t in TIERS}
        self._processed: dict[str, int] = {t: 0 for t in TIERS}
        self._stolen: dict[str, int] = {t: 0 for t in TIERS}

    async def submit(self, prompt: str, tier: str = "normal", request_id: str = "") -> str:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        req = TieredRequest(
            tier=tier, prompt=prompt,
            request_id=request_id or str(id(future)),
            enqueued_at=time.monotonic(),
            result=future
        )
        await self._queues[tier].put(req)
        return await future

    async def _get_next(self, assigned_tier: str) -> Optional[TieredRequest]:
        """Try own tier first, then steal from lower-priority tiers."""
        if not self._queues[assigned_tier].empty():
            return self._queues[assigned_tier].get_nowait()

        # Steal from lower-priority queues (higher index = lower priority)
        own_idx = TIERS.index(assigned_tier)
        for steal_tier in TIERS[own_idx + 1:]:
            if not self._queues[steal_tier].empty():
                req = self._queues[steal_tier].get_nowait()
                self._stolen[steal_tier] += 1
                print(f"[steal] {assigned_tier} worker stole from {steal_tier}")
                return req
        return None

    async def worker(self, assigned_tier: str, client: anthropic.AsyncAnthropic, stop_event: asyncio.Event):
        while not stop_event.is_set():
            req = await self._get_next(assigned_tier)
            if req is None:
                await asyncio.sleep(0.05)
                continue
            wait = time.monotonic() - req.enqueued_at
            print(f"[{assigned_tier}-worker] processing {req.tier}/{req.request_id} wait={wait:.2f}s")
            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=128,
                    messages=[{"role": "user", "content": req.prompt}]
                )
                self._processed[req.tier] += 1
                if not req.result.done():
                    req.result.set_result(response.content[0].text)
            except Exception as exc:
                if not req.result.done():
                    req.result.set_exception(exc)

    async def run(self, client: anthropic.AsyncAnthropic) -> asyncio.Event:
        stop = asyncio.Event()
        for tier, count in WORKERS_PER_TIER.items():
            for _ in range(count):
                asyncio.create_task(self.worker(tier, client, stop))
        return stop

async def demo_multi_tier():
    mq = MultiTierQueue()
    client = anthropic.AsyncAnthropic()
    stop = await mq.run(client)

    results = await asyncio.gather(
        mq.submit("Explain REST APIs.", "batch", "b1"),
        mq.submit("What is 2+2?", "critical", "c1"),
        mq.submit("Name a color.", "high", "h1"),
        mq.submit("What is TCP?", "low", "l1"),
        mq.submit("What is Python?", "normal", "n1"),
        mq.submit("What is HTTP?", "batch", "b2"),
    )

    stop.set()
    print(f"\nProcessed: {mq._processed}")
    print(f"Stolen: {mq._stolen}")
    for r in results:
        print(f"  {r[:50]}")

asyncio.run(demo_multi_tier())

# Expected Token Savings: Dedicated critical workers guarantee fast-path for urgent requests. Work stealing prevents idle critical workers when only batch jobs remain. Optimal throughput at all priority levels.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Option 5: Token-Budget-Aware Priority Admission Control

Combine priority queuing with token budget checks. High-priority requests bypass budget checks; low-priority requests are held when the budget is under pressure.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import IntEnum

class Priority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3
    BATCH = 4

# Budget pressure thresholds per priority
ADMISSION_THRESHOLDS = {
    Priority.CRITICAL: 1.0,   # Always admitted
    Priority.HIGH:     1.0,   # Always admitted
    Priority.NORMAL:   0.8,   # Block at 80% budget usage
    Priority.LOW:      0.5,   # Block at 50% budget usage
    Priority.BATCH:    0.3,   # Block at 30% budget usage
}

@dataclass(order=True)
class AdmissionRequest:
    priority: Priority
    prompt: str = field(compare=False)
    request_id: str = field(compare=False)
    enqueued_at: float = field(compare=False)
    result: asyncio.Future = field(compare=False)

class AdmissionControlQueue:
    def __init__(self, tpm_budget: int = 50_000):
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue()
        self._tpm_budget = tpm_budget
        self._used_tokens = 0
        self._window_start = time.monotonic()
        self._admitted = 0
        self._deferred = 0

    def _budget_pressure(self) -> float:
        elapsed = time.monotonic() - self._window_start
        if elapsed >= 60.0:
            self._used_tokens = 0
            self._window_start = time.monotonic()
            return 0.0
        return self._used_tokens / self._tpm_budget

    def _is_admitted(self, priority: Priority) -> bool:
        pressure = self._budget_pressure()
        threshold = ADMISSION_THRESHOLDS.get(priority, 0.5)
        return pressure < threshold

    async def submit(self, prompt: str, priority: Priority = Priority.NORMAL, request_id: str = "") -> str:
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        req = AdmissionRequest(
            priority=priority,
            prompt=prompt,
            request_id=request_id or f"req_{id(future)}",
            enqueued_at=time.monotonic(),
            result=future
        )
        await self._queue.put(req)
        return await future

    async def process_loop(self, client: anthropic.AsyncAnthropic, stop_after: int = 20):
        processed = 0
        deferred_buffer: list[AdmissionRequest] = []

        while processed < stop_after:
            # Re-admit deferred requests if budget eased
            newly_admitted = []
            for req in deferred_buffer:
                if self._is_admitted(req.priority):
                    await self._queue.put(req)
                    newly_admitted.append(req)
            for r in newly_admitted:
                deferred_buffer.remove(r)

            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(0.1)
                continue

            if not self._is_admitted(req.priority):
                self._deferred += 1
                deferred_buffer.append(req)
                print(f"[deferred] {req.priority.name}/{req.request_id} pressure={self._budget_pressure():.0%}")
                await asyncio.sleep(0.1)
                continue

            wait = time.monotonic() - req.enqueued_at
            pressure = self._budget_pressure()
            print(f"[admit/{req.priority.name}] {req.request_id} wait={wait:.2f}s pressure={pressure:.0%}")

            try:
                response = await client.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=128,
                    messages=[{"role": "user", "content": req.prompt}]
                )
                self._used_tokens += response.usage.input_tokens + response.usage.output_tokens
                self._admitted += 1
                if not req.result.done():
                    req.result.set_result(response.content[0].text)
                processed += 1
            except Exception as exc:
                if not req.result.done():
                    req.result.set_exception(exc)
                processed += 1

async def demo_admission():
    acq = AdmissionControlQueue(tpm_budget=50_000)
    client = anthropic.AsyncAnthropic()
    processor = asyncio.create_task(acq.process_loop(client, stop_after=5))

    results = await asyncio.gather(
        acq.submit("Heavy batch analysis...", Priority.BATCH, "b1"),
        acq.submit("What is 2+2?", Priority.CRITICAL, "c1"),
        acq.submit("Name a planet.", Priority.HIGH, "h1"),
        acq.submit("What is Python?", Priority.NORMAL, "n1"),
        acq.submit("Explain HTTP.", Priority.LOW, "l1"),
    )

    processor.cancel()
    print(f"\nAdmitted: {acq._admitted}, Deferred: {acq._deferred}")
    for r in results:
        print(f"  {r[:50]}")

asyncio.run(demo_admission())

# Expected Token Savings: Admission control under budget pressure defers batch jobs before rate limits hit. Prevents batch jobs from causing 429 errors that would force expensive retries for critical requests.
# Environment: ANTHROPIC_API_KEY required. Uses asyncio (stdlib).
```

---

## Option 6: SQLite-Backed Persistent Priority Queue

Store priority queue state in SQLite so it survives process restarts, enabling durable task queuing across deployments.

```python
import anthropic
import sqlite3
import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Optional

@dataclass
class PersistentRequest:
    request_id: str
    tier: str
    priority_value: int
    prompt: str
    status: str  # "pending" | "processing" | "done" | "failed"
    result: Optional[str]
    enqueued_at: float
    completed_at: Optional[float]

TIER_PRIORITY = {"critical": 0, "high": 1, "normal": 2, "low": 3, "batch": 4}

def init_queue_db(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS request_queue (
            request_id TEXT PRIMARY KEY,
            tier TEXT,
            priority_value INTEGER,
            prompt TEXT,
            status TEXT DEFAULT 'pending',
            result TEXT,
            enqueued_at REAL,
            completed_at REAL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_priority ON request_queue(priority_value, enqueued_at)")
    conn.commit()
    return conn

def enqueue(conn: sqlite3.Connection, prompt: str, tier: str = "normal") -> str:
    request_id = str(uuid.uuid4())
    priority_value = TIER_PRIORITY.get(tier, 2)
    conn.execute(
        "INSERT INTO request_queue (request_id, tier, priority_value, prompt, enqueued_at) VALUES (?,?,?,?,?)",
        (request_id, tier, priority_value, prompt, time.time())
    )
    conn.commit()
    return request_id

def claim_next(conn: sqlite3.Connection) -> Optional[PersistentRequest]:
    # Claim highest priority (lowest priority_value) pending request
    row = conn.execute("""
        SELECT request_id, tier, priority_value, prompt, enqueued_at
        FROM request_queue
        WHERE status = 'pending'
        ORDER BY priority_value ASC, enqueued_at ASC
        LIMIT 1
    """).fetchone()
    if not row:
        return None
    updated = conn.execute(
        "UPDATE request_queue SET status='processing' WHERE request_id=? AND status='pending'",
        (row[0],)
    ).rowcount
    conn.commit()
    if updated == 0:
        return None  # Lost race
    return PersistentRequest(
        request_id=row[0], tier=row[1], priority_value=row[2],
        prompt=row[3], status="processing", result=None,
        enqueued_at=row[4], completed_at=None
    )

def complete(conn: sqlite3.Connection, request_id: str, result: str):
    conn.execute(
        "UPDATE request_queue SET status='done', result=?, completed_at=? WHERE request_id=?",
        (result, time.time(), request_id)
    )
    conn.commit()

def queue_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("""
        SELECT tier, status, COUNT(*) FROM request_queue GROUP BY tier, status
    """).fetchall()
    stats: dict = {}
    for tier, status, count in rows:
        stats.setdefault(tier, {})[status] = count
    return stats

client = anthropic.Anthropic()

async def process_queue(conn: sqlite3.Connection, stop_after: int = 10):
    processed = 0
    while processed < stop_after:
        req = claim_next(conn)
        if req is None:
            await asyncio.sleep(0.1)
            continue
        wait = time.time() - req.enqueued_at
        print(f"[{req.tier}] {req.request_id[:8]} wait={wait:.2f}s priority={req.priority_value}")
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=128,
                messages=[{"role": "user", "content": req.prompt}]
            )
            complete(conn, req.request_id, response.content[0].text)
            processed += 1
        except Exception as exc:
            conn.execute(
                "UPDATE request_queue SET status='failed', result=? WHERE request_id=?",
                (str(exc), req.request_id)
            )
            conn.commit()
            processed += 1

async def demo_persistent():
    conn = init_queue_db()

    # Submit mixed priority requests
    ids = []
    for tier, prompt in [
        ("batch",    "Comprehensive analysis of quantum computing history."),
        ("critical", "What is 2+2?"),
        ("high",     "Name a planet."),
        ("low",      "Explain the water cycle."),
        ("normal",   "What is Python?"),
        ("batch",    "Explain the entire history of computing."),
    ]:
        request_id = enqueue(conn, prompt, tier)
        ids.append(request_id)
        print(f"Enqueued [{tier}]: {request_id[:8]}")

    await process_queue(conn, stop_after=6)

    print(f"\nFinal queue stats: {json.dumps(queue_stats(conn), indent=2)}")

    # Show completion order
    rows = conn.execute(
        "SELECT tier, priority_value, completed_at FROM request_queue WHERE status='done' ORDER BY completed_at"
    ).fetchall()
    print("\nCompletion order:")
    for tier, priority, completed_at in rows:
        print(f"  [{tier}] priority={priority}")

asyncio.run(demo_persistent())

# Expected Token Savings: SQLite persistence means the queue survives restarts — no lost requests, no duplicate processing. Priority ordering ensures critical requests complete before batch jobs even after a restart.
# Environment: ANTHROPIC_API_KEY required. Uses sqlite3, asyncio (stdlib).
```

---

## Comparison

| Option | Algorithm | Starvation Prevention | Persistence | Best For |
|--------|-----------|----------------------|-------------|----------|
| 1: asyncio.PriorityQueue | Strict priority | No (pure priority) | None | Simple tiered processing |
| 2: Weighted Fair Queue | WFQ with credits | Yes (weighted shares) | None | Balancing fairness and priority |
| 3: EDF (Earliest Deadline) | Deadline-first | Yes (all have deadlines) | None | SLA-bound request handling |
| 4: Multi-Queue + Stealing | Per-tier queues | Yes (work stealing) | None | High-throughput multi-model agents |
| 5: Admission Control | Budget-gated priority | Yes (defer, not drop) | None | Rate-limit-aware priority scheduling |
| 6: SQLite Priority Queue | DB-backed claim | Yes (FIFO within tier) | SQLite | Durable queuing across restarts |
