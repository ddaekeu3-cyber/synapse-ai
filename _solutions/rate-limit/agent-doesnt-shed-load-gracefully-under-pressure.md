---
layout: solution
title: "Agent Doesn't Shed Load Gracefully Under Pressure"
category: rate-limit
description: "When the agent is overloaded — too many concurrent requests, rate limits hit, or upstream APIs degraded — it queues all requests indefinitely, causing request latency to grow unbounded until the entire service crashes or times out."
tags: [rate-limit, load-shedding, backpressure, reliability, resilience, overload]
---

## Symptom

During a traffic spike, the agent receives 10× normal request volume. Instead of rejecting excess requests immediately with a 503, it queues them all. After 60 seconds the queue has 2,000 pending requests. Users who submitted requests early get responses after 90 seconds instead of 2 seconds. Users who submitted requests during peak wait 4 minutes. The service eventually OOMs or the queue drains so slowly that most responses arrive after the client has already timed out and retried — doubling the load.

## Root Cause

Agents without load-shedding accept every request unconditionally, assuming they can serve them all eventually. This is the thundering herd problem: under load, the queue grows faster than it drains, latency grows linearly with queue depth, and queued requests that have already timed out on the client side still consume server resources to process. Graceful load shedding — rejecting requests immediately when capacity is exceeded — is counterintuitive but necessary: it keeps latency bounded for the requests that do get served.

## Fix

### Option 1 — Semaphore-based concurrency limit with immediate rejection

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

MAX_CONCURRENT = 5    # maximum simultaneous LLM calls
_SEM = asyncio.Semaphore(MAX_CONCURRENT)

async def ask_with_load_shedding(question: str, request_id: str) -> dict:
    """
    Try to acquire the semaphore immediately.
    If unavailable (at capacity), shed the load — return 503.
    """
    acquired = await asyncio.wait_for(asyncio.shield(_SEM.acquire()), timeout=0.001) \
        if False else _SEM._value > 0   # non-blocking check

    if not acquired:
        return {
            "status":  503,
            "error":   "Service at capacity — please retry in a moment.",
            "shed":    True,
            "request": request_id,
        }

    async with _SEM:
        t0 = time.perf_counter()
        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": question}],
            )
            latency = round((time.perf_counter() - t0) * 1000)
            return {
                "status":    200,
                "answer":    r.content[0].text.strip(),
                "latency_ms": latency,
                "request":   request_id,
            }
        except Exception as e:
            return {"status": 500, "error": str(e), "request": request_id}

async def main() -> None:
    # Simulate 15 concurrent requests when capacity is 5
    questions = [f"Name a {animal}." for animal in
                 ["mammal", "bird", "fish", "reptile", "insect",
                  "plant",  "tree", "fruit", "vegetable", "mineral",
                  "planet", "star", "galaxy", "moon", "comet"]]

    t0      = time.perf_counter()
    results = await asyncio.gather(*[
        ask_with_load_shedding(q, f"req_{i:03d}")
        for i, q in enumerate(questions)
    ])
    elapsed = (time.perf_counter() - t0) * 1000

    served  = [r for r in results if r["status"] == 200]
    shed    = [r for r in results if r.get("shed")]
    errors  = [r for r in results if r["status"] == 500]

    print(f"15 requests in {elapsed:.0f}ms:")
    print(f"  Served:  {len(served)} requests")
    print(f"  Shed:    {len(shed)} requests (immediate 503)")
    print(f"  Errors:  {len(errors)} requests")
    if served:
        avg_latency = sum(r["latency_ms"] for r in served) / len(served)
        print(f"  Avg latency (served): {avg_latency:.0f}ms")

asyncio.run(main())
```

**Expected Token Savings:** Load shedding prevents queuing 100 requests when capacity is 5 — instead of all 100 eventually consuming tokens (many after client timeout), only 5 are served and 95 get immediate 503s, saving 95 × request_tokens.
**Environment:** Async agents exposed as APIs; immediate semaphore-based rejection is the simplest and most effective load-shedding mechanism.

---

### Option 2 — Queue depth limit with deadline-aware admission control

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

MAX_QUEUE_DEPTH    = 10      # maximum queued requests beyond concurrent limit
MAX_WAIT_MS        = 2000    # reject if queued longer than 2s (client likely timed out)
MAX_CONCURRENT     = 4

_SEM   = asyncio.Semaphore(MAX_CONCURRENT)
_QUEUE_DEPTH = 0
_LOCK  = asyncio.Lock()

async def ask_with_admission_control(question: str, client_deadline_ms: float = 5000) -> dict:
    global _QUEUE_DEPTH
    request_id = f"req_{int(time.monotonic()*1000)%100000}"
    enqueue_time = time.monotonic()

    # Check queue depth — shed load if queue is full
    async with _LOCK:
        if _QUEUE_DEPTH >= MAX_QUEUE_DEPTH:
            return {"status": 503, "error": "Queue full — shedding load", "request_id": request_id}
        _QUEUE_DEPTH += 1

    try:
        # Wait for semaphore with deadline
        remaining_deadline = client_deadline_ms / 1000 - MAX_WAIT_MS / 1000
        wait_timeout = min(MAX_WAIT_MS / 1000, remaining_deadline)

        try:
            await asyncio.wait_for(_SEM.acquire(), timeout=wait_timeout)
        except asyncio.TimeoutError:
            queue_wait = (time.monotonic() - enqueue_time) * 1000
            return {
                "status":    503,
                "error":     f"Waited {queue_wait:.0f}ms — client deadline likely expired",
                "request_id": request_id,
            }

        queue_wait_ms = (time.monotonic() - enqueue_time) * 1000
        try:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{"role": "user", "content": question}],
            )
            total_ms = (time.monotonic() - enqueue_time) * 1000
            return {
                "status":      200,
                "answer":      r.content[0].text.strip(),
                "queue_wait":  round(queue_wait_ms),
                "total_ms":    round(total_ms),
                "request_id":  request_id,
            }
        finally:
            _SEM.release()
    finally:
        async with _LOCK:
            _QUEUE_DEPTH -= 1

async def main() -> None:
    questions = [f"Say: '{i}'" for i in range(20)]
    t0 = time.perf_counter()
    results = await asyncio.gather(*[ask_with_admission_control(q) for q in questions])
    elapsed = (time.perf_counter() - t0) * 1000

    served  = [r for r in results if r["status"] == 200]
    shed    = [r for r in results if r["status"] == 503]
    print(f"20 requests in {elapsed:.0f}ms: {len(served)} served, {len(shed)} shed")
    for r in served[:3]:
        print(f"  {r['request_id']}: queue={r['queue_wait']}ms total={r['total_ms']}ms")

asyncio.run(main())
```

**Expected Token Savings:** Deadline-aware admission rejects requests that would arrive after the client timeout — these requests would consume tokens producing a response that nobody reads; rejecting them immediately saves 100% of those wasted tokens.
**Environment:** Agents behind HTTP gateways with client timeouts; deadline-aware admission is the industry-standard pattern (used by Google's Dapper, Netflix's Hystrix) for latency-sensitive services.

---

### Option 3 — Priority queue: serve high-priority requests first, shed low-priority under load

```python
import asyncio
import heapq
import time
import anthropic

client = anthropic.AsyncAnthropic()

MAX_CONCURRENT = 3

class PriorityRequest:
    def __init__(self, priority: int, question: str, request_id: str) -> None:
        self.priority   = priority   # lower number = higher priority
        self.question   = question
        self.request_id = request_id
        self.enqueue_time = time.monotonic()
        self.future: asyncio.Future = asyncio.get_event_loop().create_future()

    def __lt__(self, other: "PriorityRequest") -> bool:
        return self.priority < other.priority

class PriorityLoadShedder:
    def __init__(self, max_concurrent: int = 3, max_queue: int = 20) -> None:
        self._max_concurrent = max_concurrent
        self._max_queue      = max_queue
        self._heap:   list[PriorityRequest] = []
        self._active  = 0
        self._lock    = asyncio.Lock()
        self._sem     = asyncio.Semaphore(max_concurrent)

    async def submit(self, question: str, priority: int = 5,
                     request_id: str = "") -> dict:
        req = PriorityRequest(priority, question, request_id)

        async with self._lock:
            if len(self._heap) >= self._max_queue:
                # Shed the lowest-priority request in queue (or this one if lowest)
                heapq.heappush(self._heap, req)
                worst = heapq.nlargest(1, self._heap, key=lambda r: r.priority)[0]
                self._heap.remove(worst)
                heapq.heapify(self._heap)
                if worst.request_id == request_id:
                    return {"status": 503, "error": "Shed (lowest priority)", "request_id": request_id}
                else:
                    if not worst.future.done():
                        worst.future.set_result({"status": 503, "error": "Preempted by higher priority"})
            else:
                heapq.heappush(self._heap, req)

        asyncio.create_task(self._process_next())
        return await req.future

    async def _process_next(self) -> None:
        async with self._lock:
            if not self._heap:
                return
            req = heapq.heappop(self._heap)

        async with self._sem:
            try:
                r = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=16,
                    messages=[{"role": "user", "content": req.question}],
                )
                result = {
                    "status":   200,
                    "answer":   r.content[0].text.strip(),
                    "priority": req.priority,
                    "wait_ms":  round((time.monotonic() - req.enqueue_time) * 1000),
                }
            except Exception as e:
                result = {"status": 500, "error": str(e)}
            if not req.future.done():
                req.future.set_result(result)

async def main() -> None:
    shedder = PriorityLoadShedder(max_concurrent=3, max_queue=8)

    # Mix of priorities: 1=critical, 5=normal, 9=background
    requests = [
        ("Critical: production alert", 1),
        ("Normal user query 1",        5),
        ("Background job A",           9),
        ("Critical: payment failure",  1),
        ("Normal user query 2",        5),
        ("Background job B",           9),
        ("Normal user query 3",        5),
        ("Background job C",           9),
        ("Normal user query 4",        5),
        ("Background job D",           9),
    ]

    t0 = time.perf_counter()
    results = await asyncio.gather(*[
        shedder.submit(q, p, f"req_{i}")
        for i, (q, p) in enumerate(requests)
    ])
    elapsed = (time.perf_counter() - t0) * 1000

    print(f"10 requests in {elapsed:.0f}ms:")
    for (q, p), r in zip(requests, results):
        status = r["status"]
        info   = r.get("answer", r.get("error", ""))[:40]
        print(f"  [P{p}] {q[:30]:30s} → {status} {info}")

asyncio.run(main())
```

**Expected Token Savings:** Priority shedding ensures critical requests are served even when the system is at capacity — by shedding background jobs first, the agent spends tokens on high-value requests rather than treating all work equally under load.
**Environment:** Agents handling mixed-priority workloads (critical alerts + background batch jobs); priority-based shedding is the production pattern for agents that must never drop critical requests under any load condition.

---

### Option 4 — Token-bucket rate limiter with smooth burst handling

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

class TokenBucketLimiter:
    """
    Token bucket algorithm: refills at `rate` tokens/sec up to `capacity`.
    Each request costs 1 token. If bucket is empty, request is shed.
    """

    def __init__(self, rate: float = 5.0, capacity: float = 10.0) -> None:
        self._rate     = rate       # tokens refilled per second
        self._capacity = capacity   # maximum tokens (burst size)
        self._tokens   = capacity   # start full
        self._last     = time.monotonic()
        self._lock     = asyncio.Lock()

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed       = now - self._last
            self._tokens  = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last    = now

            if self._tokens >= 1:
                self._tokens -= 1
                return True   # admitted
            return False      # shed

    @property
    def tokens_available(self) -> float:
        return round(self._tokens, 1)

_LIMITER = TokenBucketLimiter(rate=3.0, capacity=6.0)

async def ask_with_token_bucket(question: str) -> dict:
    admitted = await _LIMITER.acquire()
    if not admitted:
        return {
            "status": 429,
            "error":  "Rate limit exceeded — token bucket empty",
            "tokens_available": _LIMITER.tokens_available,
        }

    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=32,
        messages=[{"role": "user", "content": question}],
    )
    return {
        "status":  200,
        "answer":  r.content[0].text.strip(),
        "tokens_remaining": _LIMITER.tokens_available,
    }

async def main() -> None:
    # Simulate 12 requests arriving in a burst
    questions = [f"Name item #{i}" for i in range(12)]
    t0 = time.perf_counter()
    results = await asyncio.gather(*[ask_with_token_bucket(q) for q in questions])
    elapsed = (time.perf_counter() - t0) * 1000

    served = [r for r in results if r["status"] == 200]
    shed   = [r for r in results if r["status"] == 429]
    print(f"12 burst requests in {elapsed:.0f}ms:")
    print(f"  Served: {len(served)} | Shed: {len(shed)}")
    for r in results[:4]:
        print(f"  status={r['status']} tokens_left={r.get('tokens_remaining', 'N/A')}")

asyncio.run(main())
```

**Expected Token Savings:** Token bucket allows controlled bursts (up to `capacity` tokens) while enforcing a long-term rate of `rate` requests/sec — preventing sustained overload while accommodating legitimate traffic spikes; shed requests save 100% of their token cost.
**Environment:** Public-facing agents with per-endpoint rate limits; token bucket is the standard algorithm for smooth rate limiting with burst tolerance.

---

### Option 5 — Adaptive load shedding: throttle based on observed latency

```python
import asyncio
import time
import collections
import anthropic

client = anthropic.AsyncAnthropic()

class AdaptiveShedder:
    """
    Monitor p99 latency. If latency exceeds the SLO target,
    progressively increase the shed rate until latency recovers.
    """

    def __init__(self, slo_ms: float = 3000.0, window: int = 20) -> None:
        self._slo         = slo_ms
        self._latencies:  collections.deque = collections.deque(maxlen=window)
        self._shed_rate   = 0.0   # 0 = admit all, 1.0 = shed all
        self._lock        = asyncio.Lock()

    async def should_admit(self) -> bool:
        import random
        async with self._lock:
            # Probabilistic shedding based on shed_rate
            return random.random() >= self._shed_rate

    async def record_latency(self, latency_ms: float) -> None:
        async with self._lock:
            self._latencies.append(latency_ms)
            if len(self._latencies) < 5:
                return
            p99 = sorted(self._latencies)[int(len(self._latencies) * 0.99)]
            if p99 > self._slo * 1.5:
                self._shed_rate = min(0.9, self._shed_rate + 0.1)
            elif p99 > self._slo:
                self._shed_rate = min(0.5, self._shed_rate + 0.05)
            else:
                self._shed_rate = max(0.0, self._shed_rate - 0.05)
            if self._shed_rate > 0:
                print(f"  [adaptive] p99={p99:.0f}ms shed_rate={self._shed_rate:.0%}")

_ADAPTIVE = AdaptiveShedder(slo_ms=2000.0)

async def ask_adaptive(question: str) -> dict:
    if not await _ADAPTIVE.should_admit():
        return {"status": 503, "error": "Adaptive shed — system under pressure"}

    t0 = time.perf_counter()
    try:
        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": question}],
        )
        latency = (time.perf_counter() - t0) * 1000
        await _ADAPTIVE.record_latency(latency)
        return {"status": 200, "answer": r.content[0].text.strip(), "latency_ms": round(latency)}
    except Exception as e:
        await _ADAPTIVE.record_latency(5000)   # record as slow
        return {"status": 500, "error": str(e)}

async def main() -> None:
    questions = [f"Name a {animal}." for animal in
                 ["cat", "dog", "bird", "fish", "turtle",
                  "snake", "frog", "bear", "deer", "wolf"]]

    results = await asyncio.gather(*[ask_adaptive(q) for q in questions])
    served  = sum(1 for r in results if r["status"] == 200)
    shed    = sum(1 for r in results if r["status"] == 503)
    print(f"\nServed: {served} | Shed: {shed} | Shed rate: {_ADAPTIVE._shed_rate:.0%}")
    for r in results[:5]:
        print(f"  status={r['status']} {'latency='+str(r.get('latency_ms','N/A'))+'ms' if r['status']==200 else r.get('error','')[:40]}")

asyncio.run(main())
```

**Expected Token Savings:** Adaptive shedding responds to real observed latency rather than fixed thresholds — when the upstream API slows down (not just when request rate increases), the shedder automatically reduces load; this prevents token spend on calls that would timeout anyway.
**Environment:** Agents where latency SLOs are strict; adaptive load shedding is the dynamic complement to static semaphore limits, responding to changes in API performance rather than only request volume.

---

### Option 6 — Graceful degradation with fallback responses under load

```python
import asyncio
import time
import anthropic

client = anthropic.AsyncAnthropic()

MAX_CONCURRENT       = 4
DEGRADED_THRESHOLD   = 3   # switch to degraded mode when >= 3 concurrent
_ACTIVE_REQUESTS     = 0
_LOCK                = asyncio.Lock()

# Pre-computed fallback responses for common queries (free to serve)
FALLBACK_CACHE = {
    "help":    "I'm experiencing high load. Please try again in a moment for full responses.",
    "status":  "System is under high load. Core functionality available.",
    "default": "High load — simplified response mode active. Please retry shortly for full answer.",
}

async def ask_with_degraded_fallback(question: str) -> dict:
    global _ACTIVE_REQUESTS

    async with _LOCK:
        current_load = _ACTIVE_REQUESTS
        if current_load >= MAX_CONCURRENT:
            # Full capacity — shed entirely
            return {"status": 503, "error": "At capacity", "mode": "shed"}

        _ACTIVE_REQUESTS += 1
        degraded = current_load >= DEGRADED_THRESHOLD

    try:
        if degraded:
            # Degraded mode: shorter, cheaper call
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,   # much smaller
                system="Answer in one sentence maximum.",
                messages=[{"role": "user", "content": question}],
            )
            return {
                "status": 200,
                "answer": r.content[0].text.strip(),
                "mode":   "degraded",
                "load":   current_load,
            }
        else:
            # Normal mode: full quality response
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": question}],
            )
            return {
                "status": 200,
                "answer": r.content[0].text.strip(),
                "mode":   "normal",
                "load":   current_load,
            }
    finally:
        async with _LOCK:
            _ACTIVE_REQUESTS -= 1

async def main() -> None:
    questions = [f"Explain concept #{i} in detail." for i in range(8)]
    t0 = time.perf_counter()
    results = await asyncio.gather(*[ask_with_degraded_fallback(q) for q in questions])
    elapsed = (time.perf_counter() - t0) * 1000

    print(f"8 requests in {elapsed:.0f}ms:")
    for i, r in enumerate(results):
        mode   = r.get("mode", "shed")
        answer = r.get("answer", r.get("error", ""))[:50]
        print(f"  [{mode:8s}] req_{i}: {answer}")

asyncio.run(main())
```

**Expected Token Savings:** Degraded mode serves a 32-token response instead of a 256-token response (87% savings per call) during peak load, keeping the service responsive at reduced quality rather than failing entirely; this balances token cost reduction with user experience preservation.
**Environment:** User-facing agents where silent failure is worse than a degraded response; graceful degradation is the user-experience-aware complement to hard load shedding.

---

## Comparison

| Option | Response on Overload | Queue | Priority-Aware | Best For |
|---|---|---|---|---|
| 1. Semaphore immediate reject | 503 instantly | No | No | Async APIs, simplest pattern |
| 2. Deadline-aware admission | 503 after timeout | Yes (bounded) | No | Latency-sensitive APIs with timeouts |
| 3. Priority queue | 503 low-priority | Yes (priority) | Yes | Mixed-priority workloads |
| 4. Token bucket | 429 smoothly | No | No | Public APIs with burst allowance |
| 5. Adaptive latency-based | 503 auto-adjust | No | No | Unknown load patterns, SLO-driven |
| 6. Graceful degradation | 200 (reduced quality) | No | No | User-facing, UX > perfect quality |
