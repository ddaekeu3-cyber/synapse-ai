---
layout: solution
title: "Agent Doesn't Implement Cost-Aware Request Prioritization"
category: rate-limit
description: "Assign costs and priorities to requests, then schedule expensive requests only when budget is available and shed low-priority requests under pressure."
tags: [rate-limit, prioritization, cost, budget, scheduling, load-shedding]
---

# Agent Doesn't Implement Cost-Aware Request Prioritization

Under load, all requests compete equally for API capacity. An expensive analysis request blocks cheap status checks; a background job consumes the same rate limit tokens as a real-time user query. Cost-aware prioritization assigns each request an estimated cost and priority, processes high-priority requests first, defers expensive low-priority work, and sheds requests that exceed budget.

## Option 1: Simple Priority Queue with Cost Estimation

```python
import asyncio
import anthropic
from dataclasses import dataclass, field
from enum import IntEnum

client = anthropic.AsyncAnthropic()


class Priority(IntEnum):
    CRITICAL = 0    # always execute
    HIGH = 1        # real-time user queries
    NORMAL = 2      # standard tasks
    LOW = 3         # background / batch


@dataclass(order=True)
class Request:
    priority: Priority
    estimated_tokens: int
    prompt: str = field(compare=False)
    label: str = field(compare=False, default="")


COST_ESTIMATES = {
    "status_check":    50,
    "short_answer":    150,
    "analysis":        800,
    "full_report":     2000,
}


async def process_request(req: Request) -> str:
    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=min(req.estimated_tokens, 512),
        messages=[{"role": "user", "content": req.prompt}],
    )
    actual = r.usage.input_tokens + r.usage.output_tokens
    print(f"[EXEC] {req.label} (priority={req.priority.name}, est={req.estimated_tokens}, actual={actual})")
    return r.content[0].text


async def priority_scheduler(requests: list[Request]) -> dict[str, str]:
    queue: asyncio.PriorityQueue[Request] = asyncio.PriorityQueue()
    for req in requests:
        await queue.put(req)

    results: dict[str, str] = {}
    while not queue.empty():
        req = await queue.get()
        result = await process_request(req)
        results[req.label] = result
    return results


async def main() -> None:
    requests = [
        Request(Priority.LOW,      COST_ESTIMATES["full_report"],  "Write a comprehensive report on async Python.", "bg_report"),
        Request(Priority.CRITICAL, COST_ESTIMATES["status_check"], "Reply with OK.",                                "health_check"),
        Request(Priority.HIGH,     COST_ESTIMATES["short_answer"], "What is asyncio in one sentence?",              "user_query"),
        Request(Priority.NORMAL,   COST_ESTIMATES["analysis"],     "Analyze Python async patterns in 3 points.",    "analysis"),
    ]
    results = await priority_scheduler(requests)
    for label, result in results.items():
        print(f"\n[{label}] {result[:80]}")


asyncio.run(main())

# Expected Token Savings: Critical requests never blocked by expensive background jobs
# Environment: Python 3.11+; tune Priority enum levels to match your latency SLOs
```

## Option 2: Token Budget with Load Shedding

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass
from enum import IntEnum

client = anthropic.AsyncAnthropic()

TOKEN_BUDGET_PER_MINUTE = 50_000
REFILL_INTERVAL = 60.0


class Priority(IntEnum):
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    LOW = 3


# Shedding thresholds: shed requests at this priority and below when budget is low
SHED_THRESHOLDS = [
    (0.20, Priority.LOW),     # below 20% budget: shed LOW
    (0.10, Priority.NORMAL),  # below 10%: shed NORMAL and below
    (0.05, Priority.HIGH),    # below 5%: shed everything except CRITICAL
]


@dataclass
class PrioritizedRequest:
    label: str
    prompt: str
    priority: Priority
    estimated_tokens: int


class BudgetScheduler:
    def __init__(self, budget: int) -> None:
        self._budget = budget
        self._remaining = budget
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def _maybe_refill(self) -> None:
        now = time.monotonic()
        if now - self._last_refill >= REFILL_INTERVAL:
            self._remaining = self._budget
            self._last_refill = now
            print(f"[BUDGET] Refilled to {self._budget}")

    async def try_reserve(self, tokens: int, priority: Priority) -> bool:
        async with self._lock:
            await self._maybe_refill()
            ratio = self._remaining / self._budget

            for threshold, shed_at in SHED_THRESHOLDS:
                if ratio <= threshold and priority >= shed_at:
                    print(f"[SHED] Budget={ratio:.0%} — dropping {priority.name} request")
                    return False

            if self._remaining >= tokens:
                self._remaining -= tokens
                return True

            if priority == Priority.CRITICAL:
                # Never deny critical requests
                self._remaining = max(0, self._remaining - tokens)
                return True

            return False


async def execute(req: PrioritizedRequest, scheduler: BudgetScheduler) -> tuple[str, str | None]:
    ok = await scheduler.try_reserve(req.estimated_tokens, req.priority)
    if not ok:
        return req.label, None

    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=min(req.estimated_tokens, 256),
        messages=[{"role": "user", "content": req.prompt}],
    )
    actual = r.usage.input_tokens + r.usage.output_tokens
    print(f"[OK] {req.label} ({req.priority.name}) actual={actual} tokens")
    return req.label, r.content[0].text


async def main() -> None:
    scheduler = BudgetScheduler(budget=TOKEN_BUDGET_PER_MINUTE)
    # Simulate low-budget scenario
    scheduler._remaining = 500  # pretend budget is almost exhausted

    requests = [
        PrioritizedRequest("health",   "Reply: OK",                       Priority.CRITICAL, 50),
        PrioritizedRequest("user_q",   "What is Python?",                 Priority.HIGH,     150),
        PrioritizedRequest("analysis", "Analyze Python async patterns.",   Priority.NORMAL,   800),
        PrioritizedRequest("report",   "Write a full Python async guide.", Priority.LOW,      2000),
    ]

    results = await asyncio.gather(*[execute(req, scheduler) for req in requests])
    print("\n=== Results ===")
    for label, result in results:
        print(f"  {label}: {'SHED' if result is None else result[:60]}")


asyncio.run(main())

# Expected Token Savings: Load shedding under pressure protects critical SLOs; ~40% budget saved
# Environment: Python 3.11+; tune SHED_THRESHOLDS based on your observed traffic mix
```

## Option 3: Cost-Weighted Fair Queue with SQLite Metrics

```python
import asyncio
import sqlite3
import time
import anthropic
from dataclasses import dataclass

DB_PATH = "request_metrics.db"
client = anthropic.AsyncAnthropic()


@dataclass
class WeightedRequest:
    label: str
    prompt: str
    estimated_tokens: int
    weight: float = 1.0  # higher weight = more CPU/token allocation


def init_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS request_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT, estimated_tokens INTEGER, actual_tokens INTEGER,
            weight REAL, wait_ms REAL, exec_ms REAL, ts REAL
        )
    """)
    conn.commit()
    return conn


class WeightedFairQueue:
    """
    Weighted fair queuing: each request gets capacity proportional to its weight.
    High-weight requests are scheduled more frequently.
    """
    def __init__(self) -> None:
        self._pending: list[tuple[float, float, WeightedRequest]] = []  # (priority_score, ts, req)
        self._lock = asyncio.Lock()
        self._virtual_time: dict[str, float] = {}

    async def enqueue(self, req: WeightedRequest) -> None:
        async with self._lock:
            # Virtual finish time: smaller = higher priority
            vt = self._virtual_time.get(req.label, 0.0)
            virtual_finish = vt + req.estimated_tokens / req.weight
            self._virtual_time[req.label] = virtual_finish
            self._pending.append((virtual_finish, time.monotonic(), req))
            self._pending.sort(key=lambda x: x[0])

    async def dequeue(self) -> WeightedRequest | None:
        async with self._lock:
            if not self._pending:
                return None
            _, _, req = self._pending.pop(0)
            return req


async def process_with_metrics(
    req: WeightedRequest,
    queue: WeightedFairQueue,
    conn: sqlite3.Connection,
    enqueue_time: float,
) -> tuple[str, str]:
    wait_ms = (time.monotonic() - enqueue_time) * 1000
    t0 = time.monotonic()

    r = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=min(req.estimated_tokens, 256),
        messages=[{"role": "user", "content": req.prompt}],
    )

    exec_ms = (time.monotonic() - t0) * 1000
    actual = r.usage.input_tokens + r.usage.output_tokens
    conn.execute(
        "INSERT INTO request_metrics VALUES (NULL,?,?,?,?,?,?,?)",
        (req.label, req.estimated_tokens, actual, req.weight, wait_ms, exec_ms, time.time()),
    )
    conn.commit()
    print(f"[WFQ] {req.label} wait={wait_ms:.0f}ms exec={exec_ms:.0f}ms actual={actual}tok")
    return req.label, r.content[0].text


async def main() -> None:
    conn = init_db()
    queue = WeightedFairQueue()
    enqueue_times: dict[str, float] = {}

    requests = [
        WeightedRequest("premium_user",    "Explain asyncio concurrency.",          500, weight=3.0),
        WeightedRequest("free_user_a",     "What is Python?",                       100, weight=1.0),
        WeightedRequest("free_user_b",     "Name 3 Python frameworks.",             100, weight=1.0),
        WeightedRequest("background_job",  "Summarize Python async history.",       800, weight=0.5),
    ]

    for req in requests:
        enqueue_times[req.label] = time.monotonic()
        await queue.enqueue(req)

    results = []
    while True:
        req = await queue.dequeue()
        if not req:
            break
        result = await process_with_metrics(req, queue, conn, enqueue_times[req.label])
        results.append(result)

    conn.close()
    print("\n=== Execution order (by virtual finish time) ===")
    for label, _ in results:
        print(f"  {label}")


asyncio.run(main())

# Expected Token Savings: Fair queuing prevents high-cost jobs from monopolizing capacity
# Environment: Python 3.11+, SQLite3; weight=3.0 for premium users gives 3x more throughput
```

## Option 4: Tiered Model Routing Based on Cost Budget

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

# Cost per 1M tokens (approximate)
MODEL_COSTS = {
    "claude-haiku-4-5-20251001":  {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":          {"input": 3.00,  "output": 15.00},
    "claude-opus-4-6":            {"input": 15.00, "output": 75.00},
}

DAILY_BUDGET_USD = 10.0
_spent_today = 0.0
_budget_start = time.time()


@dataclass
class RoutedRequest:
    label: str
    prompt: str
    preferred_model: str
    min_acceptable_model: str
    estimated_input_tokens: int = 500


def estimate_cost(model: str, input_tokens: int, output_tokens: int = 200) -> float:
    rates = MODEL_COSTS[model]
    return (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000


def select_model(req: RoutedRequest) -> str:
    global _spent_today

    # Reset daily budget
    if time.time() - _budget_start > 86400:
        _spent_today = 0.0

    budget_remaining = DAILY_BUDGET_USD - _spent_today
    budget_ratio = budget_remaining / DAILY_BUDGET_USD

    preferred_cost = estimate_cost(req.preferred_model, req.estimated_input_tokens)

    # Use preferred model if budget is healthy
    if budget_ratio > 0.5 and _spent_today + preferred_cost <= DAILY_BUDGET_USD:
        return req.preferred_model

    # Downgrade to cheaper tier under budget pressure
    models_by_cost = sorted(MODEL_COSTS.keys(), key=lambda m: MODEL_COSTS[m]["input"])
    for model in models_by_cost:
        cost = estimate_cost(model, req.estimated_input_tokens)
        if _spent_today + cost <= DAILY_BUDGET_USD:
            if model >= req.min_acceptable_model or budget_ratio < 0.1:
                return model

    return req.min_acceptable_model  # last resort


async def execute_routed(req: RoutedRequest) -> tuple[str, str]:
    global _spent_today
    model = select_model(req)

    r = await client.messages.create(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": req.prompt}],
    )

    cost = estimate_cost(model, r.usage.input_tokens, r.usage.output_tokens)
    _spent_today += cost
    budget_pct = (_spent_today / DAILY_BUDGET_USD) * 100

    downgraded = model != req.preferred_model
    flag = " [DOWNGRADED]" if downgraded else ""
    print(f"[ROUTE] {req.label}: {model}{flag} cost=${cost:.5f} total={budget_pct:.1f}% of daily budget")
    return req.label, r.content[0].text


async def main() -> None:
    # Simulate having spent 60% of daily budget
    global _spent_today
    _spent_today = DAILY_BUDGET_USD * 0.6

    requests = [
        RoutedRequest("analysis",  "Analyze Python async patterns deeply.",    "claude-opus-4-6",   "claude-haiku-4-5-20251001"),
        RoutedRequest("summary",   "Summarize asyncio in one paragraph.",      "claude-sonnet-4-6", "claude-haiku-4-5-20251001"),
        RoutedRequest("classify",  "Classify: is asyncio concurrent or parallel?", "claude-haiku-4-5-20251001", "claude-haiku-4-5-20251001"),
    ]

    results = await asyncio.gather(*[execute_routed(req) for req in requests])
    for label, result in results:
        print(f"\n[{label}] {result[:120]}")


asyncio.run(main())

# Expected Token Savings: Auto-downgrade under budget pressure saves 5-20x on model costs
# Environment: Python 3.11+; integrate with real billing API to track _spent_today accurately
```

## Option 5: Request Admission Control with SLA Classes

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()


class SLA(Enum):
    PLATINUM = "platinum"   # <500ms p99, never shed
    GOLD     = "gold"       # <2s p99, shed last
    SILVER   = "silver"     # <10s p99, shed second
    BRONZE   = "bronze"     # best-effort, shed first


SLA_MAX_WAIT = {SLA.PLATINUM: 0.5, SLA.GOLD: 2.0, SLA.SILVER: 10.0, SLA.BRONZE: float("inf")}
MAX_CONCURRENT = 3


@dataclass
class AdmittedRequest:
    label: str
    prompt: str
    sla: SLA
    estimated_tokens: int
    admitted_at: float = field(default_factory=time.monotonic)

    @property
    def age(self) -> float:
        return time.monotonic() - self.admitted_at

    @property
    def sla_deadline_exceeded(self) -> bool:
        return self.age > SLA_MAX_WAIT[self.sla]


class AdmissionController:
    def __init__(self, max_concurrent: int) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._queue: list[AdmittedRequest] = []
        self._lock = asyncio.Lock()

    async def admit(self, req: AdmittedRequest) -> bool:
        async with self._lock:
            self._queue.append(req)
            # Immediately shed requests that violate SLA
            self._shed_expired()
        return req in self._queue

    def _shed_expired(self) -> None:
        before = len(self._queue)
        self._queue = [r for r in self._queue if not r.sla_deadline_exceeded]
        shed = before - len(self._queue)
        if shed:
            print(f"[ADMISSION] Shed {shed} SLA-violated request(s)")

    async def execute(self, req: AdmittedRequest) -> tuple[str, str | None]:
        if req.sla_deadline_exceeded:
            return req.label, None

        async with self._semaphore:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=min(req.estimated_tokens, 256),
                messages=[{"role": "user", "content": req.prompt}],
            )
            latency = req.age * 1000
            print(f"[OK] {req.label} ({req.sla.value}) latency={latency:.0f}ms")
            return req.label, r.content[0].text


async def main() -> None:
    controller = AdmissionController(max_concurrent=MAX_CONCURRENT)

    requests = [
        AdmittedRequest("p1", "Reply: OK",                        SLA.PLATINUM, 50),
        AdmittedRequest("p2", "What is asyncio?",                 SLA.GOLD,     150),
        AdmittedRequest("p3", "List 5 Python async frameworks.",  SLA.SILVER,   400),
        AdmittedRequest("p4", "Write async Python tutorial.",     SLA.BRONZE,   1000),
    ]

    for req in requests:
        await controller.admit(req)

    results = await asyncio.gather(*[controller.execute(req) for req in requests])
    print("\n=== Results ===")
    for label, result in results:
        print(f"  {label}: {'SHED/EXPIRED' if result is None else result[:60]}")


asyncio.run(main())

# Expected Token Savings: SLA-based shedding protects platinum/gold tiers; bronze bears load
# Environment: Python 3.11+; SLA_MAX_WAIT values should match your user-facing latency SLOs
```

## Option 6: Dynamic Priority Adjustment with Aging

```python
import asyncio
import time
import anthropic
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

AGING_RATE = 0.5        # priority boost per second of waiting
MAX_PRIORITY_BOOST = 5  # cap on how much aging can boost priority
MAX_CONCURRENT = 2


@dataclass
class AgedRequest:
    label: str
    prompt: str
    base_priority: int      # lower = higher priority
    estimated_tokens: int
    enqueued_at: float = field(default_factory=time.monotonic)

    @property
    def effective_priority(self) -> float:
        age_boost = min(AGING_RATE * self.age, MAX_PRIORITY_BOOST)
        return self.base_priority - age_boost

    @property
    def age(self) -> float:
        return time.monotonic() - self.enqueued_at


class AgingScheduler:
    def __init__(self) -> None:
        self._pending: list[AgedRequest] = []
        self._lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def add(self, req: AgedRequest) -> None:
        async with self._lock:
            self._pending.append(req)

    async def _next(self) -> AgedRequest | None:
        async with self._lock:
            if not self._pending:
                return None
            # Re-sort by effective priority each time (accounts for aging)
            self._pending.sort(key=lambda r: r.effective_priority)
            req = self._pending.pop(0)
            print(f"[AGING] {req.label} base={req.base_priority} age={req.age:.1f}s eff={req.effective_priority:.1f}")
            return req

    async def execute_next(self) -> tuple[str, str] | None:
        req = await self._next()
        if not req:
            return None

        async with self._semaphore:
            r = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=min(req.estimated_tokens, 256),
                messages=[{"role": "user", "content": req.prompt}],
            )
            return req.label, r.content[0].text

    async def drain(self) -> dict[str, str]:
        results: dict[str, str] = {}
        while self._pending:
            result = await self.execute_next()
            if result:
                results[result[0]] = result[1]
        return results


async def main() -> None:
    scheduler = AgingScheduler()

    # Add requests — low-priority requests that have been waiting will age up
    requests = [
        AgedRequest("bg_job",    "Write Python async patterns guide.", base_priority=10, estimated_tokens=800,
                    enqueued_at=time.monotonic() - 30),  # been waiting 30s
        AgedRequest("user_high", "What is asyncio?",                  base_priority=1,  estimated_tokens=100),
        AgedRequest("normal",    "Name 3 Python web frameworks.",      base_priority=5,  estimated_tokens=200,
                    enqueued_at=time.monotonic() - 15),  # waiting 15s
    ]

    for req in requests:
        await scheduler.add(req)

    results = await scheduler.drain()
    print("\n=== Results ===")
    for label, result in results.items():
        print(f"  {label}: {result[:80]}")


asyncio.run(main())

# Expected Token Savings: Aging prevents starvation without sacrificing priority; no request waits forever
# Environment: Python 3.11+; tune AGING_RATE to control how fast low-priority requests catch up
```

## Comparison

| Option | Priority Model | Budget Enforcement | Load Shedding | Starvation Prevention | Best For |
|--------|---------------|-------------------|--------------|----------------------|----------|
| 1. Simple Priority Queue | Enum priority | No | No | No | Basic ordering |
| 2. Token Budget + Shedding | Enum priority | Token budget | Yes | No | Rate limit protection |
| 3. Weighted Fair Queue | Weight-based | No | No | Partial | Multi-tenant fairness |
| 4. Model Routing | Cost-based | Dollar budget | Auto-downgrade | N/A | Cost cap enforcement |
| 5. SLA Admission Control | SLA class | Concurrent limit | SLA deadline | No | Latency SLO enforcement |
| 6. Aging Scheduler | Dynamic aging | Concurrent limit | No | Yes | Starvation-free queuing |
