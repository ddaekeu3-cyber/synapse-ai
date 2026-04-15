---
layout: solution
title: "Agent Doesn't Respect API Quota Across Multiple Instances"
category: rate-limit
description: "Multiple agent instances share a single API quota but each enforces limits independently, causing total usage to exceed the quota and triggering 429 errors."
tags: [rate-limit, distributed, redis, quota, multi-instance]
---

## Symptom

A single agent instance respects its rate limit perfectly. But when the system scales to 5, 10, or 50 instances, each instance tracks its own independent counter. The result: 10 instances each allowing 100 RPM = 1,000 RPM against an API that allows 500 RPM. Half the requests fail with 429.

```python
# BROKEN: each instance has its own in-memory counter
class RateLimiter:
    def __init__(self, max_rpm: int):
        self.max_rpm = max_rpm
        self.count = 0          # local to this process — other instances don't see it
        self.window_start = time.time()

    def allow(self) -> bool:
        now = time.time()
        if now - self.window_start > 60:
            self.count = 0
            self.window_start = now
        if self.count >= self.max_rpm:
            return False
        self.count += 1
        return True

# Instance A allows 100 requests. Instance B allows 100 requests.
# Total: 200 requests against a 100 RPM API → 100 get 429'd.
```

Root causes:
- Rate limit state is held in process memory, not shared storage
- No central coordination between agent instances
- Horizontal scaling is applied without updating rate limiting architecture
- Multiple API keys reduce cost-per-call visibility but share an org-level quota

---

## Root Cause

Process-local rate limiting is correct for single-instance deployments but fundamentally broken at scale. Each process sees only its own traffic, so `N` instances each allow `max_rate` requests, for a combined `N × max_rate` — which exceeds the actual API quota by `N×`.

The solution requires shared state. Redis is the standard choice because it supports atomic increment with expiry (`INCR` + `EXPIRE`) and sorted sets for sliding window tracking. Alternatives include PostgreSQL advisory locks, Memcached, or a dedicated rate-limiting service (Kong, Nginx, AWS API Gateway).

---

## Fix

### Option 1 — Redis Fixed-Window Counter (Simplest)

Store the request count in Redis with a TTL — all instances read and increment the same counter.

```python
import anthropic
import redis
import time
import json
from typing import Optional

client = anthropic.Anthropic()

class DistributedFixedWindowLimiter:
    """
    Fixed-window rate limiter using Redis.
    All instances share the same counter — total rate is enforced globally.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        key_prefix: str,
        max_requests: int,
        window_seconds: int = 60,
    ):
        self.redis = redis_client
        self.key_prefix = key_prefix
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    def _window_key(self) -> str:
        """Key changes every window — naturally resets the counter."""
        window_id = int(time.time()) // self.window_seconds
        return f"{self.key_prefix}:{window_id}"

    def check_and_increment(self) -> tuple[bool, int, int]:
        """
        Atomically check and increment.
        Returns (allowed, current_count, remaining).
        """
        key = self._window_key()

        pipe = self.redis.pipeline()
        pipe.incr(key)
        pipe.expire(key, self.window_seconds * 2)  # 2× TTL for safety
        results = pipe.execute()

        current = results[0]
        allowed = current <= self.max_requests
        remaining = max(0, self.max_requests - current)

        return allowed, current, remaining

    def wait_until_allowed(self, poll_interval: float = 0.5) -> int:
        """Block until a request slot is available. Returns wait time in ms."""
        start = time.monotonic()
        while True:
            allowed, count, remaining = self.check_and_increment()
            if allowed:
                wait_ms = int((time.monotonic() - start) * 1000)
                return wait_ms

            # Decrement since we're not using the slot
            self.redis.decr(self._window_key())

            # Calculate time until next window
            window_id = int(time.time()) // self.window_seconds
            next_window = (window_id + 1) * self.window_seconds
            sleep_time = min(poll_interval, next_window - time.time() + 0.01)
            time.sleep(sleep_time)

# Setup
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

# All agent instances share this limiter — same Redis key, same quota
limiter = DistributedFixedWindowLimiter(
    redis_client=redis_client,
    key_prefix="anthropic:rpm",
    max_requests=500,      # actual API quota
    window_seconds=60,
)

def make_api_call_with_global_limit(messages: list[dict]) -> Optional[str]:
    """Make an Anthropic API call respecting the global rate limit."""
    allowed, count, remaining = limiter.check_and_increment()

    if not allowed:
        # Don't consume a slot we can't use
        redis_client.decr(f"anthropic:rpm:{int(time.time()) // 60}")
        print(f"  Rate limit: {count}/{limiter.max_requests} RPM used globally. Waiting...")
        wait_ms = limiter.wait_until_allowed()
        print(f"  Waited {wait_ms}ms for slot")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=256,
        messages=messages,
    )
    return response.content[0].text

# Simulate multiple "instances" sharing the same limit
def simulate_instance(instance_id: int, request_count: int):
    print(f"Instance {instance_id}: starting {request_count} requests")
    for i in range(request_count):
        result = make_api_call_with_global_limit([
            {"role": "user", "content": f"Instance {instance_id}, request {i}: ping"}
        ])
        print(f"  Instance {instance_id}, req {i}: ok")

# In real deployment: each process creates its own limiter pointing to the same Redis
simulate_instance(1, 3)
```

**Expected Token Savings:** Eliminates 429-triggered retries. Each retry wastes ~200 tokens of context re-sending.

**Environment:** Python 3.9+, `redis>=4.0`, `anthropic>=0.40.0`. Requires Redis 6+ for proper atomic operations.

---

### Option 2 — Redis Sliding Window with Sorted Set

More accurate than fixed window — prevents bursting at window boundaries.

```python
import anthropic
import redis
import time
from typing import Optional

client = anthropic.Anthropic()
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

class DistributedSlidingWindowLimiter:
    """
    Sliding window rate limiter using Redis sorted sets.
    Each request is stored with its timestamp as score.
    Old entries are pruned on each check.
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        key: str,
        max_requests: int,
        window_seconds: int = 60,
    ):
        self.redis = redis_client
        self.key = key
        self.max_requests = max_requests
        self.window_seconds = window_seconds

    def allow_request(self) -> tuple[bool, int]:
        """
        Atomically add request and check if within limit.
        Returns (allowed, current_count_in_window).
        Uses a Lua script for atomicity — no race conditions between instances.
        """
        now = time.time()
        window_start = now - self.window_seconds

        lua_script = """
        local key = KEYS[1]
        local now = tonumber(ARGV[1])
        local window_start = tonumber(ARGV[2])
        local max_requests = tonumber(ARGV[3])
        local request_id = ARGV[4]
        local window_seconds = tonumber(ARGV[5])

        -- Remove requests outside the window
        redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

        -- Count current requests in window
        local count = redis.call('ZCARD', key)

        if count < max_requests then
            -- Add this request
            redis.call('ZADD', key, now, request_id)
            redis.call('EXPIRE', key, window_seconds * 2)
            return {1, count + 1}  -- allowed, new count
        else
            return {0, count}  -- denied, current count
        end
        """

        request_id = f"{time.time():.6f}-{id(self)}"
        result = self.redis.eval(
            lua_script,
            1,  # number of keys
            self.key,
            now, window_start, self.max_requests, request_id, self.window_seconds
        )

        allowed = bool(result[0])
        count = int(result[1])
        return allowed, count

    def retry_after_seconds(self) -> float:
        """Estimate seconds until a slot opens in the sliding window."""
        now = time.time()
        window_start = now - self.window_seconds

        # Get the oldest request in the window
        oldest = self.redis.zrange(self.key, 0, 0, withscores=True)
        if not oldest:
            return 0.0

        oldest_time = oldest[0][1]
        return max(0.0, oldest_time + self.window_seconds - now + 0.1)

# All agent instances use same Redis key
limiter = DistributedSlidingWindowLimiter(
    redis_client=redis_client,
    key="anthropic:sliding:rpm",
    max_requests=500,
    window_seconds=60,
)

def throttled_anthropic_call(
    messages: list[dict],
    max_retries: int = 3,
) -> Optional[str]:
    """Make API call with global sliding-window rate limiting."""

    for attempt in range(max_retries):
        allowed, count = limiter.allow_request()

        if allowed:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                messages=messages,
            )
            return response.content[0].text

        # Calculate precise wait time
        wait = limiter.retry_after_seconds()
        print(f"  Global limit reached ({count}/500 RPM). Waiting {wait:.1f}s...")
        time.sleep(wait)

    return None  # exhausted retries

result = throttled_anthropic_call([{"role": "user", "content": "Hello"}])
print(result)
```

**Expected Token Savings:** Sliding window prevents burst-at-boundary failures, eliminating retry loops that each cost 100-300 tokens.

**Environment:** Python 3.9+, `redis>=4.0` with Lua scripting support, `anthropic>=0.40.0`.

---

### Option 3 — Token Bucket with Redis and Atomic Refill

Implements a token bucket in Redis — allows bursting up to bucket capacity while enforcing long-term rate.

```python
import anthropic
import redis
import time
import json

client = anthropic.Anthropic()
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

TOKEN_BUCKET_LUA = """
local key_tokens = KEYS[1]
local key_last_refill = KEYS[2]

local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])   -- tokens per second
local tokens_needed = tonumber(ARGV[3])
local now = tonumber(ARGV[4])

-- Get current state
local tokens = tonumber(redis.call('GET', key_tokens) or capacity)
local last_refill = tonumber(redis.call('GET', key_last_refill) or now)

-- Refill tokens based on elapsed time
local elapsed = now - last_refill
local refill = elapsed * refill_rate
tokens = math.min(capacity, tokens + refill)

-- Try to consume tokens
if tokens >= tokens_needed then
    tokens = tokens - tokens_needed
    redis.call('SET', key_tokens, tokens)
    redis.call('SET', key_last_refill, now)
    redis.call('EXPIRE', key_tokens, 3600)
    redis.call('EXPIRE', key_last_refill, 3600)
    return {1, math.floor(tokens)}  -- allowed, remaining tokens
else
    redis.call('SET', key_tokens, tokens)
    redis.call('SET', key_last_refill, now)
    redis.call('EXPIRE', key_tokens, 3600)
    redis.call('EXPIRE', key_last_refill, 3600)
    return {0, math.floor(tokens)}  -- denied, remaining tokens
end
"""

class DistributedTokenBucket:
    def __init__(
        self,
        redis_client: redis.Redis,
        key: str,
        capacity: int,        # max burst size
        refill_rate: float,   # tokens per second
        tokens_per_request: int = 1,
    ):
        self.redis = redis_client
        self.key_tokens = f"{key}:tokens"
        self.key_last_refill = f"{key}:last_refill"
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens_per_request = tokens_per_request
        self._script = self.redis.register_script(TOKEN_BUCKET_LUA)

    def consume(self) -> tuple[bool, int]:
        """Consume tokens. Returns (allowed, remaining_tokens)."""
        result = self._script(
            keys=[self.key_tokens, self.key_last_refill],
            args=[self.capacity, self.refill_rate, self.tokens_per_request, time.time()]
        )
        return bool(result[0]), int(result[1])

    def wait_time_seconds(self, tokens_needed: int = 1) -> float:
        """Seconds until `tokens_needed` tokens are available."""
        current = float(self.redis.get(self.key_tokens) or self.capacity)
        deficit = max(0, tokens_needed - current)
        return deficit / self.refill_rate

# 500 RPM = ~8.33 requests/second, burst up to 50
bucket = DistributedTokenBucket(
    redis_client=redis_client,
    key="anthropic:token_bucket",
    capacity=50,            # allow bursts of 50
    refill_rate=500 / 60,   # 8.33 tokens/second
)

def api_call_with_token_bucket(messages: list[dict]) -> str:
    """Make API call, waiting for a token slot if needed."""
    while True:
        allowed, remaining = bucket.consume()
        if allowed:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                messages=messages,
            )
            print(f"  Request allowed. {remaining} tokens remaining in bucket.")
            return response.content[0].text

        wait = bucket.wait_time_seconds()
        print(f"  Token bucket empty. Waiting {wait:.2f}s for refill...")
        time.sleep(wait + 0.01)

result = api_call_with_token_bucket([{"role": "user", "content": "Test request"}])
print(result[:100])
```

**Expected Token Savings:** Token bucket enables legitimate bursting (faster task completion) while preventing quota exhaustion — eliminates unnecessary waits on bursty workloads.

**Environment:** Python 3.9+, `redis>=4.0` with Lua scripting, `anthropic>=0.40.0`.

---

### Option 4 — Quota Coordinator Service with HTTP API

Use a dedicated lightweight service to coordinate quota across instances — useful when Redis is not available.

```python
import anthropic
import asyncio
import time
import json
from collections import deque
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

client = anthropic.Anthropic()

# ─── Quota Coordinator (run as a separate microservice) ───────────────────────

class QuotaState:
    def __init__(self, max_rpm: int):
        self.max_rpm = max_rpm
        self.requests = deque()
        self.lock = threading.Lock()

    def allow(self) -> tuple[bool, int, float]:
        now = time.time()
        window_start = now - 60.0

        with self.lock:
            # Prune old requests
            while self.requests and self.requests[0] < window_start:
                self.requests.popleft()

            count = len(self.requests)
            if count < self.max_rpm:
                self.requests.append(now)
                return True, count + 1, 0.0
            else:
                # Calculate retry_after
                oldest = self.requests[0]
                retry_after = oldest + 60.0 - now + 0.1
                return False, count, retry_after

quota = QuotaState(max_rpm=500)

class QuotaHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/allow":
            allowed, count, retry_after = quota.allow()
            response = json.dumps({
                "allowed": allowed,
                "count": count,
                "max": quota.max_rpm,
                "retry_after": retry_after,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(response)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access logs

def start_coordinator(port: int = 8765):
    """Start the quota coordinator in a background thread."""
    server = HTTPServer(("localhost", port), QuotaHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"Quota coordinator running on port {port}")
    return server

# ─── Agent Client (each instance uses this) ───────────────────────────────────

import urllib.request

def request_quota_slot(coordinator_url: str = "http://localhost:8765") -> tuple[bool, float]:
    """Ask the coordinator for permission to make one API call."""
    try:
        req = urllib.request.Request(
            f"{coordinator_url}/allow",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            return data["allowed"], data.get("retry_after", 0.0)
    except Exception as e:
        # If coordinator is down, fail open (allow the request)
        print(f"  Coordinator unavailable ({e}), allowing request")
        return True, 0.0

def make_rate_coordinated_call(messages: list[dict]) -> str:
    """Make API call, coordinating quota with all other instances."""
    max_wait = 30.0
    waited = 0.0

    while waited < max_wait:
        allowed, retry_after = request_quota_slot()
        if allowed:
            return client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=256,
                messages=messages,
            ).content[0].text

        wait = min(retry_after, max_wait - waited)
        print(f"  Coordinator denied request. Retry after {wait:.1f}s")
        time.sleep(wait)
        waited += wait

    raise TimeoutError("Could not obtain quota slot within 30s")

# Demo
server = start_coordinator(port=8765)
time.sleep(0.1)  # let server start

result = make_rate_coordinated_call([{"role": "user", "content": "Hello"}])
print(f"Response: {result[:100]}")
```

**Expected Token Savings:** Centralized coordination prevents thundering-herd 429s that waste 3-5 retry attempts per request per instance.

**Environment:** Python 3.9+, `anthropic>=0.40.0`. No external dependencies — uses stdlib `http.server`. Replace with FastAPI for production.

---

### Option 5 — Per-Model Per-Tier Quota Tracking

Track quotas separately per model and tier — different models have different rate limits.

```python
import anthropic
import redis
import time
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()
redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)

@dataclass
class ModelQuota:
    rpm: int      # requests per minute
    tpm: int      # tokens per minute
    tpd: int      # tokens per day

# Anthropic rate limits vary by model and tier — adjust to your actual plan
MODEL_QUOTAS = {
    "claude-haiku-4-5-20251001": ModelQuota(rpm=2000, tpm=200_000, tpd=5_000_000),
    "claude-sonnet-4-6":          ModelQuota(rpm=1000, tpm=80_000,  tpd=2_000_000),
    "claude-opus-4-6":            ModelQuota(rpm=500,  tpm=40_000,  tpd=1_000_000),
}

class MultiModelQuotaManager:
    """Track and enforce quotas independently per model."""

    def __init__(self, redis_client: redis.Redis):
        self.redis = redis_client

    def _rpm_key(self, model: str) -> str:
        window = int(time.time()) // 60
        return f"quota:{model}:rpm:{window}"

    def _tpm_key(self, model: str) -> str:
        window = int(time.time()) // 60
        return f"quota:{model}:tpm:{window}"

    def _tpd_key(self, model: str) -> str:
        day = int(time.time()) // 86400
        return f"quota:{model}:tpd:{day}"

    def check_and_reserve(
        self, model: str, estimated_tokens: int
    ) -> tuple[bool, dict]:
        """
        Check if request fits within model's quota.
        Returns (allowed, quota_status).
        """
        quota = MODEL_QUOTAS.get(model)
        if not quota:
            return True, {"error": f"Unknown model: {model}"}

        pipe = self.redis.pipeline()
        pipe.incr(self._rpm_key(model))
        pipe.expire(self._rpm_key(model), 120)
        pipe.incrby(self._tpm_key(model), estimated_tokens)
        pipe.expire(self._tpm_key(model), 120)
        pipe.incrby(self._tpd_key(model), estimated_tokens)
        pipe.expire(self._tpd_key(model), 172800)  # 2 days
        results = pipe.execute()

        rpm_used = results[0]
        tpm_used = results[2]
        tpd_used = results[4]

        status = {
            "model": model,
            "rpm": f"{rpm_used}/{quota.rpm}",
            "tpm": f"{tpm_used}/{quota.tpm}",
            "tpd": f"{tpd_used}/{quota.tpd}",
        }

        # Check all limits
        if rpm_used > quota.rpm:
            self._rollback(model, estimated_tokens)
            return False, {**status, "blocked_by": "rpm"}
        if tpm_used > quota.tpm:
            self._rollback(model, estimated_tokens)
            return False, {**status, "blocked_by": "tpm"}
        if tpd_used > quota.tpd:
            self._rollback(model, estimated_tokens)
            return False, {**status, "blocked_by": "tpd"}

        return True, status

    def _rollback(self, model: str, tokens: int):
        """Roll back a reservation that was denied."""
        pipe = self.redis.pipeline()
        pipe.decr(self._rpm_key(model))
        pipe.decrby(self._tpm_key(model), tokens)
        pipe.decrby(self._tpd_key(model), tokens)
        pipe.execute()

manager = MultiModelQuotaManager(redis_client)

def smart_model_call(
    messages: list[dict],
    preferred_model: str = "claude-sonnet-4-6",
    fallback_model: str = "claude-haiku-4-5-20251001",
    estimated_tokens: int = 500,
) -> Optional[str]:
    """
    Call preferred model, fall back to cheaper model if quota is hit.
    """
    for model in [preferred_model, fallback_model]:
        allowed, status = manager.check_and_reserve(model, estimated_tokens)

        if allowed:
            print(f"  Using {model} | {status['rpm']} RPM | {status['tpm']} TPM")
            response = client.messages.create(
                model=model,
                max_tokens=min(estimated_tokens, 1024),
                messages=messages,
            )
            return response.content[0].text

        print(f"  {model} quota exceeded ({status.get('blocked_by')}): {status}")

    print("  All models at quota — request dropped")
    return None

# Test: automatically falls back from Sonnet to Haiku under quota pressure
result = smart_model_call(
    messages=[{"role": "user", "content": "Summarize this briefly: AI is transforming software."}],
    preferred_model="claude-sonnet-4-6",
    fallback_model="claude-haiku-4-5-20251001",
    estimated_tokens=200,
)
print(f"Result: {result[:100] if result else 'None'}")
```

**Expected Token Savings:** Smart model fallback uses cheaper models when quota is tight, potentially cutting token costs 3-5× while maintaining throughput.

**Environment:** Python 3.9+, `redis>=4.0`, `anthropic>=0.40.0`.

---

### Option 6 — Quota-Aware Request Queue with Priority Lanes

Buffer requests in a priority queue — high-priority requests get quota slots first; low-priority ones wait.

```python
import anthropic
import asyncio
import heapq
import time
import json
from dataclasses import dataclass, field
from typing import Any, Optional
from enum import IntEnum

client = anthropic.AsyncAnthropic()

class Priority(IntEnum):
    CRITICAL = 0   # user-facing, interactive
    HIGH = 1       # background tasks, SLA-bound
    NORMAL = 2     # batch processing
    LOW = 3        # background analytics

@dataclass(order=True)
class QueuedRequest:
    priority: int
    enqueued_at: float = field(compare=False)
    request_id: str = field(compare=False)
    messages: list = field(compare=False)
    future: asyncio.Future = field(compare=False)
    model: str = field(compare=False, default="claude-sonnet-4-6")

class PriorityQuotaQueue:
    """
    Async priority queue with rate limiting.
    High-priority requests get quota slots first.
    """

    def __init__(self, rpm: int, burst: int = 20):
        self.rpm = rpm
        self.burst = burst
        self._queue: list[QueuedRequest] = []
        self._tokens: float = burst
        self._last_refill: float = time.monotonic()
        self._lock = asyncio.Lock()
        self._worker_task: Optional[asyncio.Task] = None
        self._processed = 0
        self._dropped = 0

    async def start(self):
        self._worker_task = asyncio.create_task(self._process_loop())

    async def stop(self):
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    async def enqueue(
        self,
        messages: list,
        priority: Priority = Priority.NORMAL,
        timeout: float = 30.0,
        model: str = "claude-sonnet-4-6",
    ) -> Any:
        """Add request to queue. Blocks until result is ready or timeout."""
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        request = QueuedRequest(
            priority=int(priority),
            enqueued_at=time.monotonic(),
            request_id=f"{time.time():.6f}",
            messages=messages,
            future=future,
            model=model,
        )

        async with self._lock:
            heapq.heappush(self._queue, request)

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._dropped += 1
            raise TimeoutError(f"Request timed out after {timeout}s in queue")

    def _refill_tokens(self):
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * (self.rpm / 60))
        self._last_refill = now

    async def _process_loop(self):
        while True:
            async with self._lock:
                self._refill_tokens()

                if self._queue and self._tokens >= 1.0:
                    # Pop highest priority (lowest number) request
                    request = heapq.heappop(self._queue)
                    self._tokens -= 1.0
                else:
                    request = None

            if request is None:
                await asyncio.sleep(0.05)  # wait for tokens to refill
                continue

            # Execute the request
            try:
                wait_time = time.monotonic() - request.enqueued_at
                print(
                    f"  [queue] Processing priority={request.priority} "
                    f"after {wait_time:.2f}s wait | queue_size={len(self._queue)}"
                )
                response = await client.messages.create(
                    model=request.model,
                    max_tokens=256,
                    messages=request.messages,
                )
                request.future.set_result(response.content[0].text)
                self._processed += 1
            except Exception as e:
                request.future.set_exception(e)

queue = PriorityQuotaQueue(rpm=500, burst=20)

async def demo_priority_queue():
    await queue.start()

    # Simulate mixed-priority traffic from multiple instances
    tasks = [
        queue.enqueue(
            [{"role": "user", "content": "CRITICAL: Check system health"}],
            priority=Priority.CRITICAL
        ),
        queue.enqueue(
            [{"role": "user", "content": "LOW: Generate analytics report"}],
            priority=Priority.LOW
        ),
        queue.enqueue(
            [{"role": "user", "content": "NORMAL: Answer user question"}],
            priority=Priority.NORMAL
        ),
        queue.enqueue(
            [{"role": "user", "content": "HIGH: Process customer order"}],
            priority=Priority.HIGH
        ),
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        if isinstance(result, Exception):
            print(f"Task {i}: ERROR — {result}")
        else:
            print(f"Task {i}: {str(result)[:80]}...")

    await queue.stop()
    print(f"\nProcessed: {queue._processed} | Dropped: {queue._dropped}")

asyncio.run(demo_priority_queue())
```

**Expected Token Savings:** Priority lanes ensure interactive users never wait — low-priority batch work absorbs quota pressure, preventing SLA violations that require expensive retries.

**Environment:** Python 3.9+, `asyncio`, `anthropic>=0.40.0`.

---

## Comparison

| Option | State Store | Latency Overhead | Burst Support | Priority |
|--------|-------------|-----------------|---------------|----------|
| 1 — Redis Fixed Window | Redis | ~1ms | No | No |
| 2 — Redis Sliding Window | Redis | ~2ms | No | No |
| 3 — Redis Token Bucket | Redis | ~2ms | Yes | No |
| 4 — HTTP Coordinator | In-memory service | ~5ms | No | No |
| 5 — Per-Model Tracking | Redis | ~3ms | No | No |
| 6 — Priority Queue | In-process | ~0ms | Yes | Yes |

**Start with Option 2** (sliding window in Redis) for most deployments — it's accurate, atomic, and handles multiple instances correctly. Add **Option 5** (per-model tracking) when you use multiple model tiers. Use **Option 6** (priority queue) when different workloads compete for the same quota.
