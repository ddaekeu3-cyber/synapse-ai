---
layout: solution
title: "Agent Doesn't Implement Rate Limiting Per API Key"
category: security
description: "Enforce per-API-key rate limits so that a single abusive or misconfigured client cannot exhaust quota, cause denial of service, or drive unexpected costs."
tags: [security, rate-limiting, api-key, quota, abuse-prevention, token-bucket]
---

# Agent Doesn't Implement Rate Limiting Per API Key

## Problem

Without per-API-key rate limiting, a single client — whether malicious, buggy, or simply misconfigured — can saturate your agent's capacity, exhaust your Anthropic quota, drive unexpected costs, and degrade service for all other clients. A shared global rate limit is insufficient because it cannot distinguish between legitimate high-volume clients and abusive ones.

## Solutions

### Option 1: In-Memory Token Bucket Per Key

Classic token bucket algorithm stored in a dictionary, with configurable capacity and refill rate per key tier.

```python
import anthropic
import time
from dataclasses import dataclass, field
from threading import Lock

client = anthropic.Anthropic()

# Requests per second allowed per key tier
TIER_LIMITS = {
    "free":       1.0,
    "standard":   5.0,
    "premium":   20.0,
}
BUCKET_CAPACITY_MULTIPLIER = 5   # burst = rate * multiplier


@dataclass
class TokenBucket:
    rate: float           # tokens per second
    capacity: float
    tokens: float = field(init=False)
    last_refill: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self.last_refill = time.monotonic()

    def consume(self, n: float = 1.0) -> bool:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now
        if self.tokens >= n:
            self.tokens -= n
            return True
        return False

    def wait_time(self) -> float:
        """Seconds until next token is available."""
        deficit = 1.0 - self.tokens
        return max(0.0, deficit / self.rate)


class PerKeyRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = Lock()

    def _get_bucket(self, api_key: str, tier: str) -> TokenBucket:
        if api_key not in self._buckets:
            rate = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
            self._buckets[api_key] = TokenBucket(
                rate=rate,
                capacity=rate * BUCKET_CAPACITY_MULTIPLIER,
            )
        return self._buckets[api_key]

    def allow(self, api_key: str, tier: str = "standard") -> tuple[bool, float]:
        """Returns (allowed, retry_after_seconds)."""
        with self._lock:
            bucket = self._get_bucket(api_key, tier)
            if bucket.consume():
                return True, 0.0
            return False, round(bucket.wait_time(), 2)


limiter = PerKeyRateLimiter()


def rate_limited_chat(api_key: str, tier: str, user_message: str) -> dict:
    allowed, retry_after = limiter.allow(api_key, tier)
    if not allowed:
        return {
            "error": "rate_limit_exceeded",
            "retry_after": retry_after,
            "message": f"Rate limit exceeded for key tier '{tier}'. Retry in {retry_after}s.",
        }

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": user_message}],
    )
    return {"reply": resp.content[0].text, "api_key": api_key[:8] + "..."}


if __name__ == "__main__":
    # simulate two keys: one premium, one free
    requests = [
        ("key_premium_abc", "premium", "What is 2+2?"),
        ("key_premium_abc", "premium", "What is 3+3?"),
        ("key_free_xyz",    "free",    "Hello"),
        ("key_free_xyz",    "free",    "Hello again"),   # should be rate-limited
        ("key_free_xyz",    "free",    "Yet another"),   # should be rate-limited
    ]
    for key, tier, msg in requests:
        result = rate_limited_chat(key, tier, msg)
        if "error" in result:
            print(f"[{key[:12]}] BLOCKED retry_after={result['retry_after']}s")
        else:
            print(f"[{key[:12]}] OK: {result['reply'][:60]}")

# Expected Token Savings: Prevents runaway clients from consuming unlimited quota
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: Sliding Window Counter with Redis-Compatible Dict

Sliding window algorithm that counts requests in rolling time windows, evicting stale entries automatically.

```python
import anthropic
import time
from collections import deque
from threading import Lock
from dataclasses import dataclass, field

client = anthropic.Anthropic()

WINDOW_SECONDS = 60
MAX_REQUESTS_PER_WINDOW = {
    "free":     10,
    "standard": 60,
    "premium": 300,
}


@dataclass
class SlidingWindow:
    max_requests: int
    window_seconds: int
    timestamps: deque = field(default_factory=deque)

    def record_and_check(self) -> tuple[bool, int]:
        """Returns (allowed, current_count_in_window)."""
        now = time.monotonic()
        cutoff = now - self.window_seconds

        # evict old entries
        while self.timestamps and self.timestamps[0] < cutoff:
            self.timestamps.popleft()

        count = len(self.timestamps)
        if count < self.max_requests:
            self.timestamps.append(now)
            return True, count + 1
        return False, count

    def reset_time(self) -> float:
        """Seconds until oldest entry expires."""
        if not self.timestamps:
            return 0.0
        oldest = self.timestamps[0]
        return max(0.0, (oldest + self.window_seconds) - time.monotonic())


class SlidingWindowRateLimiter:
    def __init__(self) -> None:
        self._windows: dict[str, SlidingWindow] = {}
        self._lock = Lock()

    def check(self, api_key: str, tier: str = "standard") -> tuple[bool, dict]:
        with self._lock:
            if api_key not in self._windows:
                self._windows[api_key] = SlidingWindow(
                    max_requests=MAX_REQUESTS_PER_WINDOW.get(tier, MAX_REQUESTS_PER_WINDOW["free"]),
                    window_seconds=WINDOW_SECONDS,
                )
            window = self._windows[api_key]
            allowed, count = window.record_and_check()
            meta = {
                "count": count,
                "limit": window.max_requests,
                "window_seconds": WINDOW_SECONDS,
                "reset_in": round(window.reset_time(), 1) if not allowed else 0.0,
            }
            return allowed, meta


limiter = SlidingWindowRateLimiter()


def handle_request(api_key: str, tier: str, prompt: str) -> dict:
    allowed, meta = limiter.check(api_key, tier)
    headers = {
        "X-RateLimit-Limit":     str(meta["limit"]),
        "X-RateLimit-Remaining": str(max(0, meta["limit"] - meta["count"])),
        "X-RateLimit-Reset":     str(meta["reset_in"]),
    }

    if not allowed:
        return {
            "status": 429,
            "headers": headers,
            "error": "Too Many Requests",
            "retry_after": meta["reset_in"],
        }

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return {
        "status": 200,
        "headers": headers,
        "reply": resp.content[0].text,
    }


if __name__ == "__main__":
    # hammer a free-tier key
    key = "test_key_free_001"
    for i in range(15):
        result = handle_request(key, "free", f"Request #{i}")
        if result["status"] == 429:
            print(f"  [{i:02d}] 429 Too Many Requests — reset in {result['retry_after']}s")
        else:
            remaining = result["headers"]["X-RateLimit-Remaining"]
            print(f"  [{i:02d}] 200 OK — remaining={remaining}")

# Expected Token Savings: Blocks burst abuse while allowing legitimate bursty traffic
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Async Rate Limiter with Per-Key Queuing and Fairness

Async token bucket with a per-key request queue; excess requests wait rather than fail, up to a max queue depth.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

RATE_PER_SECOND  = 2.0    # requests per second per key
BURST_SIZE       = 5      # max burst tokens
MAX_QUEUE_DEPTH  = 10     # max waiting requests per key
QUEUE_TIMEOUT    = 30.0   # seconds before queued request is dropped


@dataclass
class AsyncTokenBucket:
    rate: float
    capacity: float
    tokens: float = field(init=False)
    last: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self.last
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                self.last = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                wait = (1.0 - self.tokens) / self.rate
                await asyncio.sleep(wait)


class AsyncPerKeyRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, AsyncTokenBucket] = {}
        self._queue_sizes: dict[str, int] = {}
        self._init_lock = asyncio.Lock()

    async def _get_bucket(self, api_key: str) -> AsyncTokenBucket:
        async with self._init_lock:
            if api_key not in self._buckets:
                self._buckets[api_key] = AsyncTokenBucket(
                    rate=RATE_PER_SECOND,
                    capacity=BURST_SIZE,
                )
                self._queue_sizes[api_key] = 0
            return self._buckets[api_key]

    async def acquire(self, api_key: str) -> bool:
        bucket = await self._get_bucket(api_key)

        # check queue depth
        if self._queue_sizes[api_key] >= MAX_QUEUE_DEPTH:
            return False   # drop — queue full

        self._queue_sizes[api_key] += 1
        try:
            await asyncio.wait_for(bucket.acquire(), timeout=QUEUE_TIMEOUT)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._queue_sizes[api_key] -= 1


limiter = AsyncPerKeyRateLimiter()


async def chat(api_key: str, request_id: int, prompt: str) -> dict:
    acquired = await limiter.acquire(api_key)
    if not acquired:
        return {"request_id": request_id, "status": "dropped", "reason": "queue_full_or_timeout"}

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return {
        "request_id": request_id,
        "status": "ok",
        "reply": resp.content[0].text[:60],
    }


async def main() -> None:
    api_key = "client_key_abc123"
    # fire 20 concurrent requests — rate limiter will queue them
    tasks = [
        chat(api_key, i, f"What is {i} + {i}?")
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)
    ok      = sum(1 for r in results if r["status"] == "ok")
    dropped = sum(1 for r in results if r["status"] == "dropped")
    print(f"OK: {ok}, Dropped: {dropped}")
    for r in results:
        print(f"  [{r['request_id']:02d}] {r['status']}: {r.get('reply', r.get('reason', ''))}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Smooths spikes, drops excess; prevents runaway concurrent costs
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Hierarchical Limits — Global, Per-Key, Per-User

Three-level rate limiting: global cap > per-key cap > per-user-within-key cap, all enforced atomically.

```python
import anthropic
import time
from threading import Lock
from dataclasses import dataclass, field

client = anthropic.Anthropic()

GLOBAL_RPS      = 50.0
KEY_RPS         = 10.0
USER_PER_KEY_RPS = 2.0
BURST_MULT       = 3


@dataclass
class Bucket:
    rate: float
    capacity: float
    tokens: float = field(init=False)
    last: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def try_consume(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class HierarchicalRateLimiter:
    def __init__(self) -> None:
        self._lock = Lock()
        self._global = Bucket(rate=GLOBAL_RPS, capacity=GLOBAL_RPS * BURST_MULT)
        self._keys: dict[str, Bucket] = {}
        self._users: dict[tuple[str, str], Bucket] = {}  # (key, user_id)

    def _key_bucket(self, api_key: str) -> Bucket:
        if api_key not in self._keys:
            self._keys[api_key] = Bucket(rate=KEY_RPS, capacity=KEY_RPS * BURST_MULT)
        return self._keys[api_key]

    def _user_bucket(self, api_key: str, user_id: str) -> Bucket:
        k = (api_key, user_id)
        if k not in self._users:
            self._users[k] = Bucket(rate=USER_PER_KEY_RPS, capacity=USER_PER_KEY_RPS * BURST_MULT)
        return self._users[k]

    def check(self, api_key: str, user_id: str) -> tuple[bool, str]:
        with self._lock:
            if not self._global.try_consume():
                return False, "global_limit"
            if not self._key_bucket(api_key).try_consume():
                return False, "key_limit"
            if not self._user_bucket(api_key, user_id).try_consume():
                return False, "user_limit"
            return True, "ok"


limiter = HierarchicalRateLimiter()


def request(api_key: str, user_id: str, prompt: str) -> dict:
    allowed, reason = limiter.check(api_key, user_id)
    if not allowed:
        return {"status": 429, "blocked_by": reason}

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"status": 200, "reply": resp.content[0].text[:60]}


if __name__ == "__main__":
    key = "org_key_001"
    users = ["user_a", "user_b", "user_c"]
    results: dict[str, int] = {"ok": 0, "global_limit": 0, "key_limit": 0, "user_limit": 0}

    for i in range(30):
        uid = users[i % len(users)]
        r = request(key, uid, f"Query {i}")
        if r["status"] == 429:
            results[r["blocked_by"]] += 1
            print(f"  [{uid}] BLOCKED by {r['blocked_by']}")
        else:
            results["ok"] += 1

    print(f"\nSummary: {results}")

# Expected Token Savings: Protects against both key-level abuse and per-user overuse within shared keys
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Cost-Aware Rate Limiting (Token Budget Per Key)

Rate-limit by token consumption rather than request count, so heavy requests count more than lightweight ones.

```python
import anthropic
import time
from threading import Lock
from dataclasses import dataclass, field

client = anthropic.Anthropic()

# Token budgets per key tier (tokens per minute)
TOKEN_BUDGET_PER_MIN = {
    "free":      5_000,
    "standard": 50_000,
    "premium": 500_000,
}
WINDOW_SECONDS = 60


@dataclass
class TokenBudgetWindow:
    budget: int
    window: int
    used: int = 0
    window_start: float = field(default_factory=time.monotonic)

    def reset_if_needed(self) -> None:
        now = time.monotonic()
        if now - self.window_start >= self.window:
            self.used = 0
            self.window_start = now

    def can_spend(self, estimated_tokens: int) -> tuple[bool, int]:
        self.reset_if_needed()
        remaining = self.budget - self.used
        if estimated_tokens <= remaining:
            return True, remaining
        return False, remaining

    def record_spend(self, actual_tokens: int) -> None:
        self.used += actual_tokens

    def reset_in(self) -> float:
        return max(0.0, self.window - (time.monotonic() - self.window_start))


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


class CostAwareRateLimiter:
    def __init__(self) -> None:
        self._windows: dict[str, TokenBudgetWindow] = {}
        self._lock = Lock()

    def _get_window(self, api_key: str, tier: str) -> TokenBudgetWindow:
        if api_key not in self._windows:
            self._windows[api_key] = TokenBudgetWindow(
                budget=TOKEN_BUDGET_PER_MIN.get(tier, TOKEN_BUDGET_PER_MIN["free"]),
                window=WINDOW_SECONDS,
            )
        return self._windows[api_key]

    def pre_check(self, api_key: str, tier: str, estimated_tokens: int) -> tuple[bool, dict]:
        with self._lock:
            w = self._get_window(api_key, tier)
            allowed, remaining = w.can_spend(estimated_tokens)
            return allowed, {
                "budget":    w.budget,
                "used":      w.used,
                "remaining": remaining,
                "reset_in":  round(w.reset_in(), 1),
            }

    def record_usage(self, api_key: str, tier: str, actual_tokens: int) -> None:
        with self._lock:
            w = self._get_window(api_key, tier)
            w.record_spend(actual_tokens)


limiter = CostAwareRateLimiter()


def cost_gated_chat(api_key: str, tier: str, prompt: str) -> dict:
    estimated = estimate_tokens(prompt) + 200   # prompt + rough output estimate
    allowed, meta = limiter.pre_check(api_key, tier, estimated)

    if not allowed:
        return {
            "status": 429,
            "error": "token_budget_exceeded",
            "budget": meta["budget"],
            "used": meta["used"],
            "reset_in": meta["reset_in"],
        }

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    actual_tokens = resp.usage.input_tokens + resp.usage.output_tokens
    limiter.record_usage(api_key, tier, actual_tokens)

    return {
        "status": 200,
        "reply": resp.content[0].text[:80],
        "tokens_used": actual_tokens,
        "budget_remaining": meta["remaining"] - actual_tokens,
    }


if __name__ == "__main__":
    key = "key_free_test"
    prompts = [
        "Hello, how are you?",
        "Explain quantum computing in detail." * 10,   # large prompt
        "What is 1+1?",
        "Write a 500-word essay on climate change." * 5,
        "Short answer only: capital of France?",
    ]
    for p in prompts:
        result = cost_gated_chat(key, "free", p)
        if result["status"] == 429:
            print(f"  BLOCKED: used={result['used']}/{result['budget']} tokens, reset in {result['reset_in']}s")
        else:
            print(f"  OK: {result['tokens_used']} tokens used, {result['budget_remaining']} remaining")

# Expected Token Savings: Heavy queries consume more budget; prevents a single large request from blocking cheap ones
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Distributed Rate Limiter with Audit Log

Rate limiter that logs all decisions (allowed/blocked) with timestamps for security auditing and anomaly detection.

```python
import anthropic
import time
import sqlite3
import threading
from dataclasses import dataclass, field
from contextlib import contextmanager

client = anthropic.Anthropic()

DB_PATH    = ":memory:"   # use a file path for persistence
RATE_RPS   = 3.0
BURST_SIZE = 10


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS rate_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                ts         REAL NOT NULL,
                api_key    TEXT NOT NULL,
                allowed    INTEGER NOT NULL,
                reason     TEXT,
                tokens_est INTEGER
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_key_ts ON rate_log(api_key, ts)")


init_db()


@dataclass
class Bucket:
    rate: float
    capacity: float
    tokens: float = field(init=False)
    last: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def try_consume(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class AuditedRateLimiter:
    def __init__(self) -> None:
        self._buckets: dict[str, Bucket] = {}
        self._lock = threading.Lock()

    def _log(self, api_key: str, allowed: bool, reason: str, tokens_est: int) -> None:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO rate_log(ts, api_key, allowed, reason, tokens_est) VALUES (?,?,?,?,?)",
                (time.time(), api_key, int(allowed), reason, tokens_est),
            )

    def check(self, api_key: str, tokens_est: int = 0) -> tuple[bool, str]:
        with self._lock:
            if api_key not in self._buckets:
                self._buckets[api_key] = Bucket(rate=RATE_RPS, capacity=BURST_SIZE)
            allowed = self._buckets[api_key].try_consume()

        reason = "ok" if allowed else "rate_limit"
        threading.Thread(
            target=self._log,
            args=(api_key, allowed, reason, tokens_est),
            daemon=True,
        ).start()
        return allowed, reason

    def get_audit_summary(self, api_key: str, last_seconds: int = 60) -> dict:
        cutoff = time.time() - last_seconds
        with get_db() as conn:
            rows = conn.execute(
                "SELECT allowed, COUNT(*) as cnt FROM rate_log WHERE api_key=? AND ts>=? GROUP BY allowed",
                (api_key, cutoff),
            ).fetchall()
        total = sum(r["cnt"] for r in rows)
        blocked = sum(r["cnt"] for r in rows if not r["allowed"])
        return {
            "api_key": api_key,
            "window_seconds": last_seconds,
            "total_requests": total,
            "blocked": blocked,
            "allowed": total - blocked,
            "block_rate": round(blocked / total, 3) if total else 0.0,
        }


limiter = AuditedRateLimiter()


def secure_chat(api_key: str, prompt: str) -> dict:
    est = len(prompt) // 4 + 200
    allowed, reason = limiter.check(api_key, tokens_est=est)

    if not allowed:
        return {"status": 429, "reason": reason}

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return {"status": 200, "reply": resp.content[0].text[:60]}


if __name__ == "__main__":
    key = "api_key_client_a"
    for i in range(20):
        r = secure_chat(key, f"Question number {i}")
        status = r["status"]
        print(f"  [{i:02d}] {status}")
        time.sleep(0.1)

    time.sleep(0.5)   # let async log writes complete
    summary = limiter.get_audit_summary(key, last_seconds=120)
    print(f"\nAudit for {key}:")
    print(f"  Total={summary['total_requests']}, Allowed={summary['allowed']}, "
          f"Blocked={summary['blocked']}, BlockRate={summary['block_rate']:.1%}")

# Expected Token Savings: Audit trail enables detecting abuse patterns and tuning limits proactively
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Algorithm | Burst Handling | Fairness | Audit | Best For |
|--------|-----------|---------------|----------|-------|----------|
| 1 | Token bucket (in-memory) | Yes | Per-key | No | Simple single-process services |
| 2 | Sliding window | Smooth | Per-key | Headers | REST APIs with standard rate-limit headers |
| 3 | Async token bucket + queue | Yes (queued) | Per-key | No | High-concurrency async services |
| 4 | Hierarchical (global/key/user) | Yes | Multi-level | No | Multi-tenant platforms |
| 5 | Cost-aware token budget | No | Per-key by cost | Partial | Cost-sensitive production APIs |
| 6 | Token bucket + SQLite audit | Yes | Per-key | Full | Security-sensitive, compliance environments |
