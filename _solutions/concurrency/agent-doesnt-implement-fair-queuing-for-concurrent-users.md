---
title: "Agent Doesn't Implement Fair Queuing for Concurrent Users"
slug: agent-doesnt-implement-fair-queuing-for-concurrent-users
category: concurrency
tags: [fairness, queuing, concurrency, rate-limiting, scheduling, asyncio, anthropic-sdk]
description: >
  When multiple users submit requests simultaneously the agent processes them
  in FIFO order, allowing a single user who submits many requests to starve
  all other users. Without fair queuing, power users monopolize throughput and
  normal users experience unbounded wait times.
symptoms:
  - A single user submitting 20 requests blocks all others for minutes
  - p99 latency varies wildly depending on which user happens to be active
  - No per-user concurrency limit — one tenant can exhaust the entire API quota
  - Queue depth is shared globally with no per-user isolation
related_solutions:
  - agent-doesnt-implement-load-shedding-under-overload
  - agent-doesnt-implement-request-deduplication-for-concurrent-callers
  - agent-doesnt-implement-cooperative-cancellation-with-structured-concurrency
---

## Problem

FIFO queuing is trivially unfair: whoever submits the most requests dominates
throughput. Fair queuing distributes slots equitably across users. There are
several classic algorithms — Weighted Fair Queuing, Deficit Round Robin,
token-bucket per user, max-concurrency per user — that balance throughput
while still allowing users who are idle to "donate" their unused capacity to
active ones.

---

## Solution 1 — Per-User Concurrency Cap (Simplest)

Give each user a `Semaphore` that limits how many of their requests run
concurrently. Surplus requests wait without blocking other users.

```python
import anthropic
import asyncio
from collections import defaultdict


MAX_CONCURRENT_PER_USER = 2
_user_semaphores: dict[str, asyncio.Semaphore] = defaultdict(
    lambda: asyncio.Semaphore(MAX_CONCURRENT_PER_USER)
)


async def fair_create(
    user_id: str,
    messages: list,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
) -> str:
    sem = _user_semaphores[user_id]
    async with sem:
        print(f"[fair] user={user_id} running  ({MAX_CONCURRENT_PER_USER - sem._value} slots used)")
        client = anthropic.AsyncAnthropic()
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages
        )
        return resp.content[0].text


async def demo_per_user_cap():
    # User A submits 4 requests, user B submits 2
    tasks = [
        fair_create("alice", [{"role": "user", "content": f"Alice Q{i}: define idempotency."}])
        for i in range(4)
    ] + [
        fair_create("bob", [{"role": "user", "content": f"Bob Q{i}: define consistency."}])
        for i in range(2)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    print(f"Completed {len(results)} requests")


asyncio.run(demo_per_user_cap())
```

---

## Solution 2 — Round-Robin Scheduler Across Active Users

Maintain one `asyncio.Queue` per active user. A central dispatcher pops one
request at a time from each non-empty user queue in round-robin order,
ensuring every user gets equal turns regardless of queue depth.

```python
import anthropic
import asyncio
from collections import deque
from dataclasses import dataclass, field


@dataclass
class UserRequest:
    user_id:  str
    messages: list
    model:    str
    future:   asyncio.Future


class RoundRobinScheduler:
    def __init__(self, workers: int = 3):
        self._user_queues: dict[str, asyncio.Queue] = {}
        self._active_users: deque[str] = deque()
        self._lock = asyncio.Lock()
        self._work_available = asyncio.Event()
        self._workers = workers

    async def submit(self, user_id: str, messages: list, model: str = "claude-sonnet-4-6") -> str:
        loop = asyncio.get_running_loop()
        fut  = loop.create_future()
        req  = UserRequest(user_id=user_id, messages=messages, model=model, future=fut)
        async with self._lock:
            if user_id not in self._user_queues:
                self._user_queues[user_id] = asyncio.Queue()
                self._active_users.append(user_id)
            await self._user_queues[user_id].put(req)
        self._work_available.set()
        return await fut

    async def _next_request(self) -> UserRequest | None:
        async with self._lock:
            for _ in range(len(self._active_users)):
                if not self._active_users:
                    break
                uid = self._active_users[0]
                self._active_users.rotate(-1)
                q = self._user_queues.get(uid)
                if q and not q.empty():
                    req = q.get_nowait()
                    if q.empty():
                        del self._user_queues[uid]
                        try:
                            self._active_users.remove(uid)
                        except ValueError:
                            pass
                    return req
        return None

    async def _worker(self) -> None:
        client = anthropic.AsyncAnthropic()
        while True:
            await self._work_available.wait()
            req = await self._next_request()
            if req is None:
                self._work_available.clear()
                continue
            try:
                resp = await client.messages.create(
                    model=req.model, max_tokens=512, messages=req.messages
                )
                req.future.set_result(resp.content[0].text)
            except Exception as e:
                req.future.set_exception(e)

    async def start(self) -> None:
        for _ in range(self._workers):
            asyncio.create_task(self._worker())


_scheduler = RoundRobinScheduler(workers=3)


async def demo_round_robin():
    await _scheduler.start()

    # Alice submits 4, Bob submits 2, Carol submits 3
    tasks = (
        [_scheduler.submit("alice", [{"role": "user", "content": f"Alice Q{i}: define caching."}]) for i in range(4)] +
        [_scheduler.submit("bob",   [{"role": "user", "content": f"Bob Q{i}: define sharding."}])   for i in range(2)] +
        [_scheduler.submit("carol", [{"role": "user", "content": f"Carol Q{i}: define replication."}]) for i in range(3)]
    )
    results = await asyncio.gather(*tasks, return_exceptions=True)
    print(f"All {len(results)} requests completed")


asyncio.run(demo_round_robin())
```

---

## Solution 3 — Weighted Fair Queue (WFQ) by User Tier

Assign each user a weight based on their subscription tier. Premium users get
2x the slots; enterprise gets 4x. The scheduler runs a weighted round-robin:
premium users get 2 turns per cycle, enterprise 4, free users 1.

```python
import anthropic
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field


TIER_WEIGHTS = {"free": 1, "pro": 2, "enterprise": 4}


@dataclass
class WFQRequest:
    user_id: str
    tier:    str
    messages: list
    future:  asyncio.Future


class WeightedFairQueue:
    def __init__(self, concurrency: int = 4):
        self._queues: dict[str, asyncio.Queue] = defaultdict(asyncio.Queue)
        self._user_tier: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._sem  = asyncio.Semaphore(concurrency)
        self._has_work = asyncio.Event()

    async def submit(self, user_id: str, tier: str, messages: list) -> str:
        loop = asyncio.get_running_loop()
        fut  = loop.create_future()
        async with self._lock:
            self._user_tier[user_id] = tier
            await self._queues[user_id].put(WFQRequest(user_id, tier, messages, fut))
        self._has_work.set()
        return await fut

    def _build_schedule(self) -> list[str]:
        """Build one scheduling cycle honouring tier weights."""
        schedule = []
        for uid, q in list(self._queues.items()):
            if not q.empty():
                tier   = self._user_tier.get(uid, "free")
                weight = TIER_WEIGHTS.get(tier, 1)
                schedule.extend([uid] * weight)
        return schedule

    async def _dispatch_loop(self) -> None:
        client = anthropic.AsyncAnthropic()
        while True:
            await self._has_work.wait()
            async with self._lock:
                schedule = self._build_schedule()
            if not schedule:
                self._has_work.clear()
                continue
            for uid in schedule:
                async with self._lock:
                    q = self._queues.get(uid)
                    if not q or q.empty():
                        continue
                    req = q.get_nowait()
                asyncio.create_task(self._execute(client, req))
                await asyncio.sleep(0)   # yield

    async def _execute(self, client: anthropic.AsyncAnthropic, req: WFQRequest) -> None:
        async with self._sem:
            try:
                resp = await client.messages.create(
                    model="claude-sonnet-4-6", max_tokens=256, messages=req.messages
                )
                req.future.set_result(resp.content[0].text)
                print(f"[wfq] user={req.user_id} tier={req.tier} done")
            except Exception as e:
                req.future.set_exception(e)

    def start(self) -> None:
        asyncio.create_task(self._dispatch_loop())


_wfq = WeightedFairQueue(concurrency=3)


async def demo_wfq():
    _wfq.start()
    tasks = (
        [_wfq.submit("alice",       "free",       [{"role": "user", "content": f"Free Q{i}: what is DNS?"}]) for i in range(2)] +
        [_wfq.submit("bob",         "pro",         [{"role": "user", "content": f"Pro Q{i}: what is CDN?"}])  for i in range(2)] +
        [_wfq.submit("enterprise1", "enterprise", [{"role": "user", "content": f"Ent Q{i}: what is BGP?"}])  for i in range(2)]
    )
    results = await asyncio.gather(*tasks, return_exceptions=True)
    print(f"Completed {len(results)} requests across tiers")


asyncio.run(demo_wfq())
```

---

## Solution 4 — Deficit Round Robin (DRR) to Handle Variable Request Sizes

Deficit Round Robin extends round-robin by tracking how many tokens each user
has "owed" across rounds. Users who submitted large (expensive) requests
recently have their deficit deducted before they get another turn, preventing
token-heavy users from dominating.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field
from collections import deque


QUANTUM = 500   # tokens per round per user


@dataclass
class DRRRequest:
    user_id:     str
    messages:    list
    est_tokens:  int
    future:      asyncio.Future


class DeficitRoundRobin:
    def __init__(self, quantum: int = QUANTUM, concurrency: int = 3):
        self._quantum = quantum
        self._queues:   dict[str, deque]  = {}
        self._deficits: dict[str, int]    = {}
        self._order:    deque[str]        = deque()
        self._lock      = asyncio.Lock()
        self._sem       = asyncio.Semaphore(concurrency)
        self._has_work  = asyncio.Event()

    async def submit(self, user_id: str, messages: list, est_tokens: int = 256) -> str:
        loop = asyncio.get_running_loop()
        fut  = loop.create_future()
        req  = DRRRequest(user_id=user_id, messages=messages,
                          est_tokens=est_tokens, future=fut)
        async with self._lock:
            if user_id not in self._queues:
                self._queues[user_id]   = deque()
                self._deficits[user_id] = 0
                self._order.append(user_id)
            self._queues[user_id].append(req)
        self._has_work.set()
        return await fut

    async def _run_loop(self) -> None:
        client = anthropic.AsyncAnthropic()
        while True:
            await self._has_work.wait()
            dispatched = False
            async with self._lock:
                for _ in range(len(self._order)):
                    if not self._order:
                        break
                    uid = self._order[0]
                    self._order.rotate(-1)
                    q = self._queues.get(uid)
                    if not q:
                        continue
                    self._deficits[uid] += self._quantum
                    while q and self._deficits[uid] >= q[0].est_tokens:
                        req = q.popleft()
                        self._deficits[uid] -= req.est_tokens
                        asyncio.create_task(self._execute(client, req))
                        dispatched = True
                    if not q:
                        del self._queues[uid]
                        self._deficits.pop(uid, None)
                        try:
                            self._order.remove(uid)
                        except ValueError:
                            pass
            if not dispatched:
                self._has_work.clear()
            await asyncio.sleep(0.01)

    async def _execute(self, client: anthropic.AsyncAnthropic, req: DRRRequest) -> None:
        async with self._sem:
            try:
                resp = await client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=req.est_tokens,
                    messages=req.messages,
                )
                req.future.set_result(resp.content[0].text)
                print(f"[drr] user={req.user_id} est={req.est_tokens} actual={resp.usage.output_tokens}")
            except Exception as e:
                req.future.set_exception(e)

    def start(self) -> None:
        asyncio.create_task(self._run_loop())


_drr = DeficitRoundRobin(quantum=500, concurrency=3)


async def demo_drr():
    _drr.start()
    tasks = (
        # Alice submits large requests
        [_drr.submit("alice", [{"role": "user", "content": "Write a 300-token essay on hashing."}], est_tokens=400) for _ in range(2)] +
        # Bob submits small requests
        [_drr.submit("bob",   [{"role": "user", "content": "What is a hash?"}], est_tokens=64) for _ in range(4)]
    )
    results = await asyncio.gather(*tasks, return_exceptions=True)
    print(f"DRR completed {len(results)} requests")


asyncio.run(demo_drr())
```

---

## Solution 5 — Token-Bucket Fair Rate Limiter Per User

Each user has a token bucket that refills at a fixed rate (requests per
second). Requests that arrive when the bucket is empty wait until tokens
are available, spreading load evenly over time without hard concurrency caps.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    rate:     float   # tokens per second
    capacity: float   # max burst size
    tokens:   float = field(init=False)
    last_refill: float = field(init=False)

    def __post_init__(self):
        self.tokens = self.capacity
        self.last_refill = time.monotonic()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.last_refill = now

    def wait_time(self, cost: float = 1.0) -> float:
        """Returns seconds to wait before the request can proceed."""
        self._refill()
        if self.tokens >= cost:
            return 0.0
        return (cost - self.tokens) / self.rate

    def consume(self, cost: float = 1.0) -> None:
        self._refill()
        self.tokens -= cost


_user_buckets: dict[str, TokenBucket] = {}
_bucket_lock = asyncio.Lock()


def _get_bucket(user_id: str, rate: float = 1.0, capacity: float = 3.0) -> TokenBucket:
    if user_id not in _user_buckets:
        _user_buckets[user_id] = TokenBucket(rate=rate, capacity=capacity)
    return _user_buckets[user_id]


async def rate_limited_create(
    user_id: str,
    messages: list,
    model: str = "claude-sonnet-4-6",
    rate_rps: float = 1.0,
    burst: float = 3.0,
) -> str:
    bucket = _get_bucket(user_id, rate=rate_rps, capacity=burst)

    async with _bucket_lock:
        wait = bucket.wait_time(cost=1.0)

    if wait > 0:
        print(f"[token-bucket] user={user_id} waiting {wait:.2f}s for rate limit")
        await asyncio.sleep(wait)

    async with _bucket_lock:
        bucket.consume(1.0)

    client = anthropic.AsyncAnthropic()
    resp = await client.messages.create(
        model=model, max_tokens=256, messages=messages
    )
    print(f"[token-bucket] user={user_id} done  tokens_left={bucket.tokens:.1f}")
    return resp.content[0].text


async def demo_token_bucket():
    # Simulate Alice bursting 5 requests instantly
    tasks = [
        rate_limited_create("alice", [{"role": "user", "content": f"Q{i}: define latency."}])
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = [r for r in results if isinstance(r, str)]
    print(f"Alice: {len(ok)}/5 completed")


asyncio.run(demo_token_bucket())
```

---

## Solution 6 — Max-Min Fair Share Scheduler

Implement the classic max-min fairness algorithm: allocate slots to the user
with the smallest current share first. Users with no pending requests donate
their share back to the pool. This guarantees each user gets at least
`total_slots / active_users` concurrent slots.

```python
import anthropic
import asyncio
from dataclasses import dataclass, field


@dataclass
class MMFRequest:
    user_id:  str
    messages: list
    future:   asyncio.Future


class MaxMinFairScheduler:
    """
    Maintains per-user in-flight counts and a global concurrency limit.
    Next slot always goes to the user with fewest in-flight requests.
    """

    def __init__(self, total_slots: int = 6):
        self._total_slots = total_slots
        self._in_flight:  dict[str, int]           = {}
        self._pending:    dict[str, list[MMFRequest]] = {}
        self._global_sem  = asyncio.Semaphore(total_slots)
        self._lock        = asyncio.Lock()
        self._has_pending = asyncio.Event()

    async def submit(self, user_id: str, messages: list) -> str:
        loop = asyncio.get_running_loop()
        fut  = loop.create_future()
        req  = MMFRequest(user_id=user_id, messages=messages, future=fut)
        async with self._lock:
            self._pending.setdefault(user_id, []).append(req)
            self._in_flight.setdefault(user_id, 0)
        self._has_pending.set()
        return await fut

    def _pick_next(self) -> MMFRequest | None:
        """Select request from the user with fewest in-flight requests."""
        eligible = {uid: reqs for uid, reqs in self._pending.items() if reqs}
        if not eligible:
            return None
        # Pick user with minimum in-flight count
        uid = min(eligible, key=lambda u: self._in_flight.get(u, 0))
        req = eligible[uid].pop(0)
        if not self._pending[uid]:
            del self._pending[uid]
        return req

    async def _dispatch_loop(self) -> None:
        client = anthropic.AsyncAnthropic()
        while True:
            await self._has_pending.wait()
            async with self._lock:
                req = self._pick_next()
                if req is None:
                    self._has_pending.clear()
                    continue
                self._in_flight[req.user_id] = self._in_flight.get(req.user_id, 0) + 1
            asyncio.create_task(self._execute(client, req))

    async def _execute(self, client: anthropic.AsyncAnthropic, req: MMFRequest) -> None:
        async with self._global_sem:
            try:
                resp = await client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=256,
                    messages=req.messages,
                )
                req.future.set_result(resp.content[0].text)
            except Exception as e:
                req.future.set_exception(e)
            finally:
                async with self._lock:
                    self._in_flight[req.user_id] = max(0, self._in_flight.get(req.user_id, 1) - 1)
                self._has_pending.set()   # wake dispatcher — more slots free
                print(f"[mmf] user={req.user_id}  in_flight={self._in_flight}")

    def start(self) -> None:
        asyncio.create_task(self._dispatch_loop())


_mmf = MaxMinFairScheduler(total_slots=4)


async def demo_mmf():
    _mmf.start()
    # Alice 5, Bob 2, Carol 3 — MMF should give each a proportional share
    tasks = (
        [_mmf.submit("alice", [{"role": "user", "content": f"Alice {i}: what is TCP?"}]) for i in range(5)] +
        [_mmf.submit("bob",   [{"role": "user", "content": f"Bob {i}: what is UDP?"}])   for i in range(2)] +
        [_mmf.submit("carol", [{"role": "user", "content": f"Carol {i}: what is TLS?"}]) for i in range(3)]
    )
    await asyncio.gather(*tasks, return_exceptions=True)
    print("Max-min fair scheduling complete")


asyncio.run(demo_mmf())
```

---

## Comparison

| Approach | Fairness guarantee | Handles burst | Variable request size | Tier support | Complexity |
|---|---|---|---|---|---|
| Per-user concurrency cap | Equal slots | Yes (waits) | No | No | Very low |
| Round-robin scheduler | Equal turns | Yes | No | No | Low |
| Weighted fair queue | Tier-proportional turns | Yes | No | Yes | Medium |
| Deficit Round Robin | Token-proportional turns | Yes | Yes | No | Medium |
| Token-bucket rate limiter | Rate-proportional | Yes (burst allowed) | No | No | Low |
| Max-min fair share | Max-min optimal | Yes | No | No | High |

**Rule of thumb:**
- Simple multi-tenant API → per-user concurrency cap (Solution 1) deployed in minutes
- Equal-priority users with variable load → round-robin (Solution 2)
- SaaS with subscription tiers → weighted fair queue (Solution 3)
- Mixed small/large requests → Deficit Round Robin (Solution 4) prevents large-request starvation
- Smooth rate limiting (no queue needed) → token-bucket (Solution 5)
- Academic optimality required → max-min fair share (Solution 6)
