---
layout: solution
title: "Agent doesn't share rate-limit state across worker processes"
category: rate-limit
description: "Each worker process maintains its own in-memory rate limiter, so 10 workers each allow 60 RPM independently — sending 600 RPM to an API with a 60 RPM limit and triggering cascading 429s."
tags: [rate-limiting, multi-process, worker, shared-state, redis, distributed]
---

## Symptom

The agent is deployed as 8 Gunicorn/Celery workers. Each worker has an in-memory `TokenBucket(rate=1.0)` — designed to cap throughput at 1 request/second. Under load, the API returns 429s at roughly 8 req/s, not 1 req/s. Scaling from 4 to 8 workers doubles the 429 rate instead of increasing throughput.

## Root Cause

In-process rate limiters live in the Python heap of one worker. Other workers have their own independent limiter instances, each unaware of the others. The combined request rate equals `workers × per_worker_limit`, which easily exceeds the API's actual quota. The problem is invisible in single-worker development and only emerges under production load.

---

## Option 1 — Redis atomic sliding-window limiter (shared across all workers)

**Use Redis `ZADD`/`ZREMRANGEBYSCORE` to count requests in a sliding window across all processes atomically.**

```python
import time
import anthropic
import redis

client = anthropic.Anthropic()
r = redis.Redis(host="localhost", port=6379, decode_responses=True)

RATE_KEY = "agent:api:rpm"
MAX_RPM   = 60     # global limit, shared across all workers
WINDOW    = 60.0   # seconds


def acquire_global_slot() -> float:
    """Block until a request slot is available. Returns wait time."""
    while True:
        now = time.time()
        pipe = r.pipeline()
        # Remove entries older than the window
        pipe.zremrangebyscore(RATE_KEY, 0, now - WINDOW)
        # Count current entries
        pipe.zcard(RATE_KEY)
        # Add this request attempt
        unique_id = f"{now:.6f}-{id(pipe)}"
        pipe.zadd(RATE_KEY, {unique_id: now})
        pipe.expire(RATE_KEY, int(WINDOW) + 5)
        _, count, _, _ = pipe.execute()

        if count < MAX_RPM:
            return 0.0   # slot acquired

        # Window full — remove the entry we just added and wait
        r.zrem(RATE_KEY, unique_id)
        oldest = r.zrange(RATE_KEY, 0, 0, withscores=True)
        if oldest:
            wait = WINDOW - (now - oldest[0][1])
            time.sleep(max(0.05, wait))
        else:
            time.sleep(0.1)


def call_api(prompt: str) -> str:
    acquire_global_slot()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


# Each worker calls the same function — Redis enforces the global 60 RPM cap
if __name__ == "__main__":
    for i in range(5):
        print(call_api(f"Task {i}: explain concept {i}"))
```

**Expected Token Savings:** Eliminating 429 cascades across 8 workers saves the exponential back-off retry cost — typically 2–4× the base token spend during a sustained overload event.

**Environment:** Multi-worker deployments (Gunicorn, Celery, multiprocessing); Redis 5+; `redis-py>=4.0`.

---

## Option 2 — Redis token bucket with Lua script (atomic, no race condition)

**Implement the token bucket algorithm as a Lua script that runs atomically on Redis — no TOCTOU race between workers.**

```python
import time
import anthropic
import redis

client = anthropic.Anthropic()
r = redis.Redis(host="localhost", port=6379, decode_responses=True)

# Atomic Lua token bucket: fills at `rate` tokens/sec, max `capacity`
BUCKET_SCRIPT = r.register_script("""
local key      = KEYS[1]
local rate     = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local now      = tonumber(ARGV[3])
local tokens_needed = tonumber(ARGV[4])

local data = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens     = tonumber(data[1]) or capacity
local last_refill = tonumber(data[2]) or now

-- Refill tokens based on elapsed time
local elapsed = now - last_refill
tokens = math.min(capacity, tokens + elapsed * rate)

if tokens >= tokens_needed then
    tokens = tokens - tokens_needed
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, 3600)
    return 1   -- acquired
else
    redis.call('HMSET', key, 'tokens', tokens, 'last_refill', now)
    redis.call('EXPIRE', key, 3600)
    return 0   -- denied
end
""")

BUCKET_KEY = "agent:token_bucket"
RATE        = 1.0    # tokens per second (= 60 RPM)
CAPACITY    = 10.0   # burst capacity


def acquire(tokens: float = 1.0, max_wait: float = 30.0) -> bool:
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        now = time.time()
        result = BUCKET_SCRIPT(keys=[BUCKET_KEY], args=[RATE, CAPACITY, now, tokens])
        if result == 1:
            return True
        wait = tokens / RATE
        time.sleep(min(wait, 0.5))
    return False


def call_api(prompt: str) -> str | None:
    if not acquire():
        return None
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


print(call_api("What is the capital of France?"))
```

**Expected Token Savings:** Lua atomicity prevents "thundering herd" where multiple workers all pass the check simultaneously and collectively exceed the limit — eliminates 100% of cross-worker 429s from race conditions.

**Environment:** High-concurrency deployments where multiple workers race to acquire permits; Redis 2.6+ (Lua support).

---

## Option 3 — Celery rate limiting via `task_annotations`

**For Celery workers, use built-in `rate_limit` annotation — Celery enforces it across all workers via the broker.**

```python
import anthropic
from celery import Celery

app = Celery("agent", broker="redis://localhost:6379/0")
client = anthropic.Anthropic()

# Celery enforces this globally across all workers via Redis/RabbitMQ broker
app.conf.task_annotations = {
    "agent.tasks.llm_call": {"rate_limit": "60/m"},   # 60 calls per minute, globally
}


@app.task(bind=True, max_retries=3)
def llm_call(self, prompt: str) -> str:
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except anthropic.RateLimitError as exc:
        raise self.retry(exc=exc, countdown=10)


# Dispatch 200 tasks — Celery throttles them to 60/min globally
if __name__ == "__main__":
    results = [llm_call.delay(f"Task {i}") for i in range(200)]
    for r in results[:5]:
        print(r.get(timeout=120))
```

**Celery worker startup:**
```bash
celery -A agent.tasks worker --concurrency=8 --loglevel=info
```

**Expected Token Savings:** Broker-level rate limiting is applied before a task executes — no wasted API calls or 429 retries from workers that have already been given a task slot.

**Environment:** Celery-based agent deployments; works with Redis, RabbitMQ, and SQS brokers; zero code changes to the task logic.

---

## Option 4 — Shared memory rate limiter using `multiprocessing.Value`

**For `multiprocessing`-based workers on a single host, use a `multiprocessing.Value` counter protected by a `Lock` — no Redis required.**

```python
import multiprocessing
import time
import anthropic

client = anthropic.Anthropic()

# Shared counters across all forked workers
_lock       = multiprocessing.Lock()
_count      = multiprocessing.Value("i", 0)    # requests in current window
_window_start = multiprocessing.Value("d", time.time())

MAX_RPM = 60
WINDOW  = 60.0


def acquire_shared_slot() -> None:
    while True:
        with _lock:
            now = time.time()
            elapsed = now - _window_start.value
            if elapsed >= WINDOW:
                # New window
                _window_start.value = now
                _count.value = 0
                elapsed = 0.0

            if _count.value < MAX_RPM:
                _count.value += 1
                return   # slot acquired

        # Window full — wait for the window to reset
        remaining = WINDOW - (time.time() - _window_start.value)
        time.sleep(max(0.1, remaining))


def worker_main(worker_id: int, prompts: list[str]) -> None:
    for prompt in prompts:
        acquire_shared_slot()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        print(f"[W{worker_id}] {response.content[0].text[:50]}")


if __name__ == "__main__":
    all_prompts = [f"Question {i}" for i in range(30)]
    chunk_size  = len(all_prompts) // 4
    workers = []
    for i in range(4):
        chunk = all_prompts[i * chunk_size : (i + 1) * chunk_size]
        p = multiprocessing.Process(target=worker_main, args=(i, chunk))
        workers.append(p)
        p.start()
    for p in workers:
        p.join()
```

**Expected Token Savings:** Single-host shared memory enforcement adds zero network overhead vs Redis — prevents the same 429 cascade while eliminating the Redis round-trip latency.

**Environment:** `multiprocessing`-based agents on a single machine (no Kubernetes); Python 3.8+; zero external dependencies.

---

## Option 5 — Nginx rate limiting as infrastructure-level gate

**Place an Nginx reverse proxy in front of all worker processes. Nginx enforces `limit_req` globally before requests reach any worker.**

```nginx
# /etc/nginx/conf.d/agent.conf
limit_req_zone $binary_remote_addr zone=agent_api:10m rate=60r/m;

upstream agent_workers {
    server 127.0.0.1:8001;
    server 127.0.0.1:8002;
    server 127.0.0.1:8003;
    server 127.0.0.1:8004;
}

server {
    listen 80;

    location /api/ask {
        limit_req zone=agent_api burst=10 nodelay;
        limit_req_status 429;
        proxy_pass http://agent_workers;
    }
}
```

```python
# Worker code — no rate limiting needed here; Nginx handles it
import anthropic
from flask import Flask, request, jsonify

app   = Flask(__name__)
client = anthropic.Anthropic()


@app.route("/api/ask", methods=["POST"])
def ask():
    data   = request.json
    prompt = data.get("prompt", "")
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return jsonify({"answer": response.content[0].text})


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8001
    app.run(port=port)
```

**Expected Token Savings:** Infrastructure-layer enforcement rejects excess requests before they consume any Python CPU or API quota — 100% of over-limit requests are denied with 0 tokens spent.

**Environment:** HTTP-serving agents behind Nginx or similar reverse proxy; particularly effective when the API client is external (webhook receiver, public API).

---

## Option 6 — Distributed rate limiter with leader election fallback

**Primary limiter in Redis; if Redis is down, fall back to a conservative per-process limit rather than failing open.**

```python
import os
import time
import anthropic
import redis as redis_lib

client = anthropic.Anthropic()

REDIS_URL      = os.getenv("REDIS_URL", "redis://localhost:6379")
GLOBAL_RPM     = 60
FALLBACK_RPM   = 5    # conservative per-process limit when Redis is down
WINDOW         = 60.0
RATE_KEY       = "agent:global_rpm"

_redis: redis_lib.Redis | None = None
_fallback_count = 0
_fallback_window_start = time.time()


def get_redis() -> redis_lib.Redis | None:
    global _redis
    if _redis is not None:
        try:
            _redis.ping()
            return _redis
        except Exception:
            _redis = None
    try:
        _redis = redis_lib.from_url(REDIS_URL, socket_connect_timeout=1)
        _redis.ping()
        return _redis
    except Exception:
        return None


def acquire() -> None:
    global _fallback_count, _fallback_window_start
    r = get_redis()

    if r:
        # Redis available: use global sliding window
        while True:
            now = time.time()
            pipe = r.pipeline()
            pipe.zremrangebyscore(RATE_KEY, 0, now - WINDOW)
            pipe.zcard(RATE_KEY)
            uid = f"{now:.6f}-{os.getpid()}"
            pipe.zadd(RATE_KEY, {uid: now})
            pipe.expire(RATE_KEY, int(WINDOW) + 5)
            _, count, _, _ = pipe.execute()

            if count < GLOBAL_RPM:
                return
            r.zrem(RATE_KEY, uid)
            oldest = r.zrange(RATE_KEY, 0, 0, withscores=True)
            wait = WINDOW - (now - oldest[0][1]) if oldest else 1.0
            time.sleep(max(0.1, wait))
    else:
        # Fallback: conservative per-process limit
        now = time.time()
        if now - _fallback_window_start >= WINDOW:
            _fallback_count = 0
            _fallback_window_start = now

        while _fallback_count >= FALLBACK_RPM:
            remaining = WINDOW - (time.time() - _fallback_window_start)
            time.sleep(max(0.1, remaining))
            now = time.time()
            if now - _fallback_window_start >= WINDOW:
                _fallback_count = 0
                _fallback_window_start = now

        _fallback_count += 1
        print(f"[fallback mode] {_fallback_count}/{FALLBACK_RPM} RPM")


def call_api(prompt: str) -> str:
    acquire()
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text


print(call_api("What is machine learning?"))
```

**Expected Token Savings:** Fail-safe fallback prevents full rate limit enforcement loss during Redis outages — limits blast radius to `workers × FALLBACK_RPM` instead of `workers × GLOBAL_RPM`.

**Environment:** Production agents where Redis availability is not guaranteed; critical for 24/7 deployments that can't tolerate Redis as a single point of failure.

---

## Comparison

| Option | Scope | Requires Redis | Works Multi-host | Complexity |
|--------|-------|---------------|-----------------|------------|
| 1. Redis sliding window | Global | Yes | Yes | Low |
| 2. Redis Lua token bucket | Global (atomic) | Yes | Yes | Medium |
| 3. Celery `rate_limit` | Per-task type | Via broker | Yes | Very Low |
| 4. `multiprocessing.Value` | Single-host | No | No | Low |
| 5. Nginx `limit_req` | HTTP ingress | No | Via upstream | Medium |
| 6. Redis + fallback | Global + safe fallback | Preferred | Yes | Medium |

**Recommended path:** For most deployments, Option 1 (Redis sliding window) gives the best bang-for-buck — shared enforcement, simple implementation, works across all worker types. Use Option 3 (Celery annotation) if you're already on Celery. Add Option 6's fallback logic to any Redis-based solution for production resilience.
