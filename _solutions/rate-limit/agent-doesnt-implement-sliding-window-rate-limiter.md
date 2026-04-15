---
layout: solution
title: "Agent Doesn't Implement Sliding Window Rate Limiter"
category: rate-limit
description: "Agents using simple fixed-window counters allow request bursts at window boundaries (the 'thundering herd' problem). A sliding window rate limiter distributes load smoothly and prevents bursty traffic from exhausting Anthropic API quotas or overwhelming downstream services."
tags: [rate-limit, sliding-window, token-bucket, throttling, asyncio, redis, sqlite, concurrency]
---

## Problem

Fixed-window counters reset at the start of each minute/hour, allowing clients to send N requests at second 59 and another N at second 61 — effectively 2N requests in 2 seconds. Sliding window algorithms prevent this by measuring the rate over any rolling time window. Without a sliding window limiter, agents hit Anthropic's rate limits in bursts, cause retry storms, and degrade service for all users sharing the quota.

## Solutions

### Option 1: In-Memory Sliding Window with deque

```python
import anthropic
import asyncio
import time
from collections import deque
from threading import Lock

client = anthropic.Anthropic()

class SlidingWindowLimiter:
    """
    Tracks timestamps of recent requests in a deque.
    Allows at most `max_requests` within any rolling `window_seconds`.
    """
    def __init__(self, max_requests: int, window_seconds: float):
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = Lock()

    def is_allowed(self) -> bool:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            # Evict timestamps outside the window
            while self._timestamps and self._timestamps[0] < cutoff:
                self._timestamps.popleft()
            if len(self._timestamps) < self._max:
                self._timestamps.append(now)
                return True
            return False

    def wait_time(self) -> float:
        """Seconds until a request slot opens."""
        with self._lock:
            if len(self._timestamps) < self._max:
                return 0.0
            oldest = self._timestamps[0]
            return max(0.0, oldest + self._window - time.time())

    def acquire(self):
        """Block until a slot is available."""
        while not self.is_allowed():
            wait = self.wait_time()
            time.sleep(max(0.01, wait))

limiter = SlidingWindowLimiter(max_requests=5, window_seconds=10.0)

def call_claude(prompt: str) -> str:
    limiter.acquire()
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

if __name__ == "__main__":
    prompts = [f"What is {i}+{i}?" for i in range(12)]
    for i, p in enumerate(prompts):
        t0 = time.time()
        result = call_claude(p)
        print(f"[{i+1:02d}] elapsed={time.time()-t0:.2f}s | {result.strip()[:40]}")

# Expected Token Savings: prevents quota overruns that trigger 429s requiring expensive retries
# Environment: single-process agents; thread-safe for multi-threaded workers
```

### Option 2: Async Sliding Window with asyncio.Lock

```python
import anthropic
import asyncio
import time
from collections import deque

client = anthropic.AsyncAnthropic()

class AsyncSlidingWindowLimiter:
    def __init__(self, max_requests: int, window_seconds: float):
        self._max = max_requests
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def _evict(self, now: float):
        cutoff = now - self._window
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    async def acquire(self):
        while True:
            async with self._lock:
                now = time.time()
                await self._evict(now)
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return
                wait = self._timestamps[0] + self._window - now
            await asyncio.sleep(max(0.005, wait))

async def call_claude(limiter: AsyncSlidingWindowLimiter, prompt: str, idx: int) -> str:
    await limiter.acquire()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    print(f"  [{idx:02d}] {text.strip()[:50]}")
    return text

async def main():
    limiter = AsyncSlidingWindowLimiter(max_requests=5, window_seconds=10.0)
    tasks = [
        call_claude(limiter, f"Count to {i}", i)
        for i in range(1, 13)
    ]
    t0 = time.time()
    results = await asyncio.gather(*tasks)
    print(f"\nAll {len(results)} requests completed in {time.time()-t0:.1f}s")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: async gather with rate limiting prevents 429 storms on parallel calls
# Environment: asyncio agents; gather() fans out N coroutines, limiter queues them safely
```

### Option 3: SQLite-Backed Sliding Window for Multi-Process Agents

```python
import anthropic
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

DB = Path("/tmp/rate_limit.db")
client = anthropic.Anthropic()

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS request_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            service TEXT NOT NULL,
            ts REAL NOT NULL
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_service_ts ON request_log(service, ts)")
    con.commit()
    con.close()

@contextmanager
def sliding_window_acquire(service: str, max_requests: int, window_seconds: float):
    """
    SQLite-backed acquire: safe across multiple processes or workers.
    Uses BEGIN EXCLUSIVE to prevent race conditions.
    """
    while True:
        con = sqlite3.connect(DB, timeout=5.0)
        try:
            con.execute("BEGIN EXCLUSIVE")
            now = time.time()
            cutoff = now - window_seconds

            # Count requests in window
            (count,) = con.execute(
                "SELECT COUNT(*) FROM request_log WHERE service=? AND ts >= ?",
                (service, cutoff),
            ).fetchone()

            if count < max_requests:
                con.execute(
                    "INSERT INTO request_log (service, ts) VALUES (?, ?)",
                    (service, now),
                )
                # Prune old entries
                con.execute(
                    "DELETE FROM request_log WHERE service=? AND ts < ?",
                    (service, cutoff - window_seconds),
                )
                con.commit()
                break
            else:
                oldest_ts = con.execute(
                    "SELECT MIN(ts) FROM request_log WHERE service=? AND ts >= ?",
                    (service, cutoff),
                ).fetchone()[0]
                con.rollback()
                wait = oldest_ts + window_seconds - now
                time.sleep(max(0.05, wait))
        finally:
            con.close()
    yield

def call_claude(prompt: str) -> str:
    with sliding_window_acquire("anthropic", max_requests=5, window_seconds=10.0):
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

if __name__ == "__main__":
    init_db()
    for i in range(8):
        t0 = time.time()
        r = call_claude(f"What is {i} squared?")
        print(f"[{i+1}] {time.time()-t0:.2f}s | {r.strip()[:40]}")

# Expected Token Savings: prevents duplicate request storms across worker processes sharing the same API key
# Environment: multi-process agents (gunicorn workers, Celery); SQLite EXCLUSIVE lock ensures consistency
```

### Option 4: Per-User Sliding Window with Quota Tiers

```python
import anthropic
import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass

@dataclass
class QuotaTier:
    max_requests: int
    window_seconds: float

TIERS = {
    "free": QuotaTier(max_requests=3, window_seconds=60.0),
    "pro": QuotaTier(max_requests=20, window_seconds=60.0),
    "enterprise": QuotaTier(max_requests=100, window_seconds=60.0),
}

class PerUserSlidingWindow:
    def __init__(self):
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._lock = asyncio.Lock()

    async def check_and_acquire(self, user_id: str, tier: str) -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        quota = TIERS.get(tier, TIERS["free"])
        now = time.time()
        cutoff = now - quota.window_seconds

        async with self._lock:
            dq = self._windows[user_id]
            while dq and dq[0] < cutoff:
                dq.popleft()

            if len(dq) < quota.max_requests:
                dq.append(now)
                return True, 0.0
            retry_after = dq[0] + quota.window_seconds - now
            return False, max(0.0, retry_after)

limiter = PerUserSlidingWindow()
client = anthropic.AsyncAnthropic()

async def handle_request(user_id: str, tier: str, prompt: str) -> dict:
    allowed, retry_after = await limiter.check_and_acquire(user_id, tier)
    if not allowed:
        return {
            "error": "rate_limited",
            "retry_after": round(retry_after, 1),
            "user": user_id,
        }
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=64,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"result": resp.content[0].text, "user": user_id}

async def main():
    requests = [
        ("alice", "free", "Hello!"),
        ("alice", "free", "Hello again!"),
        ("alice", "free", "Third request"),
        ("alice", "free", "Fourth (should be blocked)"),
        ("bob", "pro", "My first request"),
        ("bob", "pro", "My second request"),
    ]
    results = await asyncio.gather(*[handle_request(*r) for r in requests])
    for req, res in zip(requests, results):
        user, tier, _ = req
        if "error" in res:
            print(f"[{user}/{tier}] BLOCKED: retry in {res['retry_after']}s")
        else:
            print(f"[{user}/{tier}] OK: {res['result'].strip()[:40]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: free-tier users blocked before hitting API; only paying-tier traffic reaches Claude
# Environment: multi-tenant APIs; per-user isolation prevents one user exhausting shared quota
```

### Option 5: Smooth Rate Limiting with Token Leaky Bucket

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

class LeakyBucket:
    """
    Leaky bucket: tokens drain at a constant rate. Requests consume tokens.
    Smooths bursty traffic into a constant outflow rate.
    """
    def __init__(self, capacity: float, leak_rate: float):
        """
        capacity: max tokens in bucket
        leak_rate: tokens per second drained from bucket
        """
        self._capacity = capacity
        self._leak_rate = leak_rate
        self._tokens = capacity
        self._last_leak = time.monotonic()
        self._lock = asyncio.Lock()

    async def _leak(self):
        now = time.monotonic()
        elapsed = now - self._last_leak
        self._tokens = min(self._capacity, self._tokens + elapsed * self._leak_rate)
        self._last_leak = now

    async def acquire(self, tokens: float = 1.0):
        while True:
            async with self._lock:
                await self._leak()
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self._leak_rate
            await asyncio.sleep(wait)

    @property
    def fill_level(self) -> float:
        return self._tokens / self._capacity

bucket = LeakyBucket(capacity=10.0, leak_rate=2.0)  # 2 req/s steady state

async def rate_limited_call(prompt: str, idx: int, cost: float = 1.0) -> str:
    await bucket.acquire(tokens=cost)
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": prompt}],
    )
    text = resp.content[0].text
    print(f"  [{idx:02d}] bucket={bucket.fill_level:.0%} | {text.strip()[:40]}")
    return text

async def main():
    t0 = time.time()
    tasks = [
        rate_limited_call(f"Say 'response {i}' only.", i)
        for i in range(10)
    ]
    await asyncio.gather(*tasks)
    print(f"\nCompleted in {time.time()-t0:.1f}s (expected ~{10/2:.0f}s at 2 req/s)")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: smooth outflow prevents burst-then-429 cycles; each 429 costs a retry token
# Environment: high-throughput agents; leak_rate tunable to stay below Anthropic TPM/RPM limits
```

### Option 6: Distributed Sliding Window with In-Process Redis-Like State

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class WindowState:
    """Shared state simulating a Redis sorted-set sliding window."""
    max_requests: int
    window_ms: int  # milliseconds
    timestamps: list[float] = field(default_factory=list)

    def _evict(self, now_ms: float):
        cutoff = now_ms - self.window_ms
        self.timestamps = [t for t in self.timestamps if t >= cutoff]

    def acquire(self, now_ms: float) -> tuple[bool, float]:
        self._evict(now_ms)
        if len(self.timestamps) < self.max_requests:
            self.timestamps.append(now_ms)
            return True, 0.0
        retry_after_ms = self.timestamps[0] + self.window_ms - now_ms
        return False, retry_after_ms / 1000.0

_states: dict[str, WindowState] = {}
_lock = asyncio.Lock()

async def acquire_slot(key: str, max_requests: int, window_seconds: float) -> tuple[bool, float]:
    async with _lock:
        if key not in _states:
            _states[key] = WindowState(max_requests=max_requests, window_ms=int(window_seconds * 1000))
        now_ms = time.time() * 1000
        return _states[key].acquire(now_ms)

async def call_with_rate_limit(key: str, prompt: str, max_req: int = 5, window: float = 10.0) -> str:
    while True:
        allowed, retry_after = await acquire_slot(key, max_req, window)
        if allowed:
            break
        await asyncio.sleep(retry_after + 0.01)

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=48,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text

async def main():
    t0 = time.time()
    tasks = [
        call_with_rate_limit("global", f"What is {i} * {i}?")
        for i in range(12)
    ]
    results = await asyncio.gather(*tasks)
    print(f"Completed {len(results)} requests in {time.time()-t0:.1f}s")
    for i, r in enumerate(results):
        print(f"  [{i+1:02d}] {r.strip()[:50]}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: sliding window prevents burst-induced 429s; key-namespaced for multiple API clients
# Environment: in-process async agents; replace _states dict with Redis ZADD/ZREMRANGEBYSCORE for true distributed use
```

## Comparison

| Option | Storage | Multi-Process | Per-User | Smoothing |
|--------|---------|--------------|---------|---------|
| 1 — deque + threading.Lock | In-memory | No | No | None (step function) |
| 2 — asyncio.Lock + deque | In-memory | No | No | None |
| 3 — SQLite EXCLUSIVE | Disk | Yes | No | None |
| 4 — Per-user async deque | In-memory | No | Yes (tier-based) | None |
| 5 — Leaky bucket | In-memory | No | No | Yes (constant drain) |
| 6 — Sorted-set simulation | In-memory | No | By key | None |
