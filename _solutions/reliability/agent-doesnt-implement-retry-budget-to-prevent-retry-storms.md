---
layout: solution
title: "Agent Doesn't Implement Retry Budget to Prevent Retry Storms"
category: reliability
description: "Cap total retry attempts across all concurrent operations so that a downstream outage doesn't cause an exponential surge of retry traffic that worsens the incident."
tags: [reliability, retry, retry-storm, backoff, budget, rate-limiting, resilience]
---

# Agent Doesn't Implement Retry Budget to Prevent Retry Storms

## Problem

When many agent requests fail simultaneously — due to a downstream outage, rate limit spike, or model overload — naive per-request retry logic causes every failed request to retry independently. This creates a "retry storm": the combined retry traffic can exceed the original load by 3–10x, making a partial outage into a total one and delaying recovery. A retry budget constrains total retry capacity across all concurrent operations.

## Solutions

### Option 1: Global Retry Token Bucket

Maintain a shared token bucket that every retry must acquire from. When the bucket is exhausted, retries are dropped rather than executed.

```python
import anthropic
import asyncio
import time
import random
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

RETRY_BUDGET_RPS      = 5.0   # max retries per second globally
RETRY_BUDGET_BURST    = 10    # burst capacity
MAX_RETRIES_PER_REQ   = 3


@dataclass
class RetryBudget:
    rate: float
    capacity: float
    tokens: float = field(init=False)
    last: float = field(default_factory=time.monotonic)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
            self.last = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    @property
    def available(self) -> float:
        return self.tokens


budget = RetryBudget(rate=RETRY_BUDGET_RPS, capacity=RETRY_BUDGET_BURST)


async def call_with_budget_retry(request_id: int, prompt: str) -> dict:
    base_delay = 0.5
    last_error = ""

    for attempt in range(MAX_RETRIES_PER_REQ + 1):
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return {
                "request_id": request_id,
                "status": "ok",
                "attempts": attempt + 1,
                "reply": resp.content[0].text[:60],
            }
        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            last_error = str(e)
            if attempt >= MAX_RETRIES_PER_REQ:
                break

            # must acquire retry budget token
            got_budget = await budget.acquire()
            if not got_budget:
                return {
                    "request_id": request_id,
                    "status": "budget_exhausted",
                    "attempts": attempt + 1,
                    "error": "Retry budget exhausted — dropping to protect downstream",
                }

            delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
            await asyncio.sleep(delay)

    return {
        "request_id": request_id,
        "status": "failed",
        "attempts": MAX_RETRIES_PER_REQ + 1,
        "error": last_error,
    }


async def main() -> None:
    # simulate 20 concurrent requests (some may encounter transient errors)
    tasks = [
        call_with_budget_retry(i, f"Summarize topic {i % 5} briefly.")
        for i in range(20)
    ]
    results = await asyncio.gather(*tasks)
    ok       = sum(1 for r in results if r["status"] == "ok")
    dropped  = sum(1 for r in results if r["status"] == "budget_exhausted")
    failed   = sum(1 for r in results if r["status"] == "failed")
    print(f"OK={ok}, BudgetDropped={dropped}, Failed={failed}")
    for r in results:
        print(f"  [{r['request_id']:02d}] {r['status']} (attempts={r['attempts']})")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Prevents 3–10x retry amplification during outages
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 2: Per-Error-Class Retry Budget with Counters

Track retry budgets separately for different error types (rate limit, server error, timeout), allowing more retries for transient errors and fewer for sustained overload.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()


class ErrorClass(Enum):
    RATE_LIMIT   = "rate_limit"
    SERVER_ERROR = "server_error"
    TIMEOUT      = "timeout"
    UNKNOWN      = "unknown"


# Max retries allowed per error class per sliding window
ERROR_CLASS_BUDGET = {
    ErrorClass.RATE_LIMIT:   20,  # transient, allow more
    ErrorClass.SERVER_ERROR: 10,  # may indicate degradation
    ErrorClass.TIMEOUT:       5,  # timeouts can pile up fast
    ErrorClass.UNKNOWN:       3,
}
WINDOW_SECONDS = 30


@dataclass
class PerClassBudget:
    max_retries: int
    window: int
    timestamps: list = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self) -> bool:
        async with self._lock:
            now = time.monotonic()
            self.timestamps = [t for t in self.timestamps if now - t < self.window]
            if len(self.timestamps) < self.max_retries:
                self.timestamps.append(now)
                return True
            return False

    def remaining(self) -> int:
        now = time.monotonic()
        active = [t for t in self.timestamps if now - t < self.window]
        return max(0, self.max_retries - len(active))


class ErrorClassBudgetManager:
    def __init__(self) -> None:
        self._budgets = {
            cls: PerClassBudget(max_retries=budget, window=WINDOW_SECONDS)
            for cls, budget in ERROR_CLASS_BUDGET.items()
        }

    @staticmethod
    def classify(exc: Exception) -> ErrorClass:
        if isinstance(exc, anthropic.RateLimitError):
            return ErrorClass.RATE_LIMIT
        if isinstance(exc, anthropic.APIStatusError) and exc.status_code >= 500:
            return ErrorClass.SERVER_ERROR
        if isinstance(exc, asyncio.TimeoutError):
            return ErrorClass.TIMEOUT
        return ErrorClass.UNKNOWN

    async def acquire(self, exc: Exception) -> tuple[bool, ErrorClass]:
        cls = self.classify(exc)
        allowed = await self._budgets[cls].acquire()
        return allowed, cls

    def status(self) -> dict:
        return {
            cls.value: self._budgets[cls].remaining()
            for cls in ErrorClass
        }


manager = ErrorClassBudgetManager()


async def resilient_call(request_id: int, prompt: str) -> dict:
    max_attempts = 4
    for attempt in range(max_attempts):
        try:
            resp = await asyncio.wait_for(
                client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{"role": "user", "content": prompt}],
                ),
                timeout=10.0,
            )
            return {"request_id": request_id, "status": "ok", "attempts": attempt + 1,
                    "reply": resp.content[0].text[:60]}

        except Exception as exc:
            if attempt == max_attempts - 1:
                return {"request_id": request_id, "status": "failed", "error": str(exc)[:60]}

            allowed, error_class = await manager.acquire(exc)
            if not allowed:
                return {
                    "request_id": request_id,
                    "status":     "budget_exhausted",
                    "error_class": error_class.value,
                    "budget_status": manager.status(),
                }

            backoff = 0.5 * (2 ** attempt)
            await asyncio.sleep(backoff)

    return {"request_id": request_id, "status": "failed"}


async def main() -> None:
    results = await asyncio.gather(
        *[resilient_call(i, f"Query {i}") for i in range(15)]
    )
    for r in results:
        print(f"  [{r['request_id']:02d}] {r['status']}")
    print(f"\nBudget remaining: {manager.status()}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Rate-limit retries are allowed more room; timeout retries cut off quickly
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 3: Retry Budget with Admission Control and Jitter

Combine a retry budget with admission control: when retries are scarce, only high-priority requests are allowed to retry.

```python
import anthropic
import asyncio
import random
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

GLOBAL_RETRY_BUDGET  = 15   # max concurrent retry slots
JITTER_MAX_SEC       = 1.0


@dataclass
class AdmissionControlledBudget:
    total_slots: int
    used: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self, priority: str = "normal") -> bool:
        async with self._lock:
            # high-priority can use up to 100% of slots
            # normal priority can only use up to 70%
            limit = self.total_slots if priority == "high" else int(self.total_slots * 0.7)
            if self.used < limit:
                self.used += 1
                return True
            return False

    async def release(self) -> None:
        async with self._lock:
            self.used = max(0, self.used - 1)

    def utilization(self) -> float:
        return self.used / self.total_slots if self.total_slots else 0.0


budget = AdmissionControlledBudget(total_slots=GLOBAL_RETRY_BUDGET)


async def call_with_admission(
    request_id: int,
    prompt: str,
    priority: str = "normal",
    max_retries: int = 3,
) -> dict:
    for attempt in range(max_retries + 1):
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return {
                "request_id": request_id,
                "priority":   priority,
                "status":     "ok",
                "attempts":   attempt + 1,
                "reply":      resp.content[0].text[:60],
            }
        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            if attempt >= max_retries:
                return {"request_id": request_id, "status": "failed", "error": str(e)[:60]}

            admitted = await budget.acquire(priority=priority)
            if not admitted:
                return {
                    "request_id":  request_id,
                    "priority":    priority,
                    "status":      "admission_denied",
                    "utilization": f"{budget.utilization():.0%}",
                }

            jitter = random.uniform(0, JITTER_MAX_SEC)
            backoff = 0.5 * (2 ** attempt) + jitter
            try:
                await asyncio.sleep(backoff)
            finally:
                await budget.release()

    return {"request_id": request_id, "status": "failed"}


async def main() -> None:
    tasks = (
        [call_with_admission(i, f"High priority task {i}", priority="high") for i in range(5)]
        + [call_with_admission(i + 5, f"Normal task {i}", priority="normal") for i in range(15)]
    )
    results = await asyncio.gather(*tasks)
    by_status: dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
        print(f"  [{r['request_id']:02d}][{r['priority']}] {r['status']}")
    print(f"\nSummary: {by_status}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: High-priority requests keep retrying; low-priority shed load during outages
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 4: Retry Storm Detector with Automatic Backpressure

Monitor the retry rate in real time; when it exceeds a threshold, automatically engage a backpressure mode that delays all retries.

```python
import anthropic
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

STORM_THRESHOLD_RPS  = 10.0   # retries/sec that triggers backpressure
MEASURE_WINDOW       = 5.0    # seconds to measure retry rate
BACKPRESSURE_DELAY   = 2.0    # extra delay in backpressure mode


@dataclass
class StormDetector:
    threshold_rps: float
    window: float
    _retry_times: deque = field(default_factory=deque)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _backpressure: bool = False

    async def record_retry(self) -> float:
        """Record a retry attempt. Returns delay to apply (0 = normal, >0 = backpressure)."""
        async with self._lock:
            now = time.monotonic()
            self._retry_times.append(now)
            # evict old
            while self._retry_times and self._retry_times[0] < now - self.window:
                self._retry_times.popleft()

            rps = len(self._retry_times) / self.window
            self._backpressure = rps >= self.threshold_rps
            delay = BACKPRESSURE_DELAY if self._backpressure else 0.0
            return delay

    @property
    def in_backpressure(self) -> bool:
        return self._backpressure

    async def current_rps(self) -> float:
        async with self._lock:
            now = time.monotonic()
            active = [t for t in self._retry_times if now - t < self.window]
            return len(active) / self.window


detector = StormDetector(threshold_rps=STORM_THRESHOLD_RPS, window=MEASURE_WINDOW)


async def storm_safe_call(request_id: int, prompt: str, max_retries: int = 3) -> dict:
    for attempt in range(max_retries + 1):
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            return {
                "request_id": request_id,
                "status":     "ok",
                "attempts":   attempt + 1,
                "reply":      resp.content[0].text[:60],
            }
        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            if attempt >= max_retries:
                return {"request_id": request_id, "status": "failed", "error": str(e)[:60]}

            backpressure_delay = await detector.record_retry()
            rps = await detector.current_rps()

            base_delay = 0.5 * (2 ** attempt)
            total_delay = base_delay + backpressure_delay

            if backpressure_delay > 0:
                print(f"  [req {request_id:02d}] BACKPRESSURE active (rps={rps:.1f}) — delay={total_delay:.1f}s")

            await asyncio.sleep(total_delay)

    return {"request_id": request_id, "status": "failed"}


async def main() -> None:
    # simulate 30 concurrent requests to trigger storm detection
    tasks = [storm_safe_call(i, f"Question {i}") for i in range(30)]
    results = await asyncio.gather(*tasks)
    ok = sum(1 for r in results if r["status"] == "ok")
    failed = sum(1 for r in results if r["status"] != "ok")
    final_rps = await detector.current_rps()
    print(f"\nOK={ok}, Failed={failed}, Final retry RPS={final_rps:.2f}")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Auto-detects and throttles retry storms before they cause cascading failure
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 5: Retry Budget with Prometheus-Style Metrics

Track retry budget consumption as metrics (counters, gauges) for alerting and dashboards.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from threading import Lock

client = anthropic.AsyncAnthropic()

RETRY_RATE_LIMIT     = 8    # max retries per window
WINDOW_SECONDS       = 10
MAX_RETRIES_PER_REQ  = 3


@dataclass
class RetryMetrics:
    _lock: Lock = field(default_factory=Lock)
    total_attempts:      int = 0
    total_retries:       int = 0
    budget_exhausted:    int = 0
    success_on_retry:    int = 0
    failed_after_retry:  int = 0
    retry_window:        list = field(default_factory=list)

    def record_attempt(self) -> None:
        with self._lock:
            self.total_attempts += 1

    def record_retry(self) -> bool:
        """Returns True if retry is within budget."""
        now = time.monotonic()
        with self._lock:
            self.retry_window = [t for t in self.retry_window if now - t < WINDOW_SECONDS]
            if len(self.retry_window) < RETRY_RATE_LIMIT:
                self.retry_window.append(now)
                self.total_retries += 1
                return True
            self.budget_exhausted += 1
            return False

    def record_outcome(self, succeeded: bool, was_retry: bool) -> None:
        with self._lock:
            if was_retry and succeeded:
                self.success_on_retry += 1
            elif was_retry and not succeeded:
                self.failed_after_retry += 1

    def prometheus_text(self) -> str:
        with self._lock:
            retry_rps = len(self.retry_window) / WINDOW_SECONDS if self.retry_window else 0
            lines = [
                "# HELP agent_retry_total Total retry attempts",
                f"agent_retry_total {self.total_retries}",
                "# HELP agent_retry_budget_exhausted Retries dropped due to budget",
                f"agent_retry_budget_exhausted {self.budget_exhausted}",
                "# HELP agent_retry_rps Current retry rate per second",
                f"agent_retry_rps {retry_rps:.3f}",
                "# HELP agent_retry_success_total Successful recoveries via retry",
                f"agent_retry_success_total {self.success_on_retry}",
                "# HELP agent_retry_failed_total Requests that failed despite retries",
                f"agent_retry_failed_total {self.failed_after_retry}",
            ]
        return "\n".join(lines)


metrics = RetryMetrics()


async def metered_call(request_id: int, prompt: str) -> dict:
    metrics.record_attempt()
    was_retry = False

    for attempt in range(MAX_RETRIES_PER_REQ + 1):
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            metrics.record_outcome(succeeded=True, was_retry=was_retry)
            return {"request_id": request_id, "status": "ok", "attempts": attempt + 1}

        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            if attempt >= MAX_RETRIES_PER_REQ:
                metrics.record_outcome(succeeded=False, was_retry=was_retry)
                return {"request_id": request_id, "status": "failed"}

            within_budget = metrics.record_retry()
            if not within_budget:
                return {"request_id": request_id, "status": "budget_exhausted"}

            was_retry = True
            await asyncio.sleep(0.5 * (2 ** attempt))

    return {"request_id": request_id, "status": "failed"}


async def main() -> None:
    tasks = [metered_call(i, f"Prompt {i}") for i in range(25)]
    results = await asyncio.gather(*tasks)
    ok = sum(1 for r in results if r["status"] == "ok")
    print(f"Results: OK={ok}/{len(results)}\n")
    print(metrics.prometheus_text())


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Metrics reveal retry storm onset before it causes full outage
# Environment: ANTHROPIC_API_KEY must be set
```

---

### Option 6: Retry Budget with Hedging and Deadline Propagation

Combine retry budgets with request hedging: if the primary request is slow, issue a secondary request using the retry budget, and cancel whichever finishes last.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

HEDGE_AFTER_MS       = 800    # issue hedge if primary hasn't responded
DEADLINE_SECONDS     = 5.0    # absolute deadline per logical request
HEDGE_BUDGET_SLOTS   = 5      # concurrent hedge requests allowed


@dataclass
class HedgeBudget:
    slots: int
    used: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self) -> bool:
        async with self._lock:
            if self.used < self.slots:
                self.used += 1
                return True
            return False

    async def release(self) -> None:
        async with self._lock:
            self.used = max(0, self.used - 1)


hedge_budget = HedgeBudget(slots=HEDGE_BUDGET_SLOTS)


async def single_call(prompt: str, label: str) -> tuple[str, str]:
    """Returns (label, reply_text)."""
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=128,
        messages=[{"role": "user", "content": prompt}],
    )
    return label, resp.content[0].text


async def hedged_call(request_id: int, prompt: str) -> dict:
    deadline = time.monotonic() + DEADLINE_SECONDS
    start = time.monotonic()

    primary_task = asyncio.create_task(single_call(prompt, "primary"))

    # wait for hedge threshold
    hedge_wait = HEDGE_AFTER_MS / 1000
    done, _ = await asyncio.wait({primary_task}, timeout=hedge_wait)

    if done:
        label, reply = primary_task.result()
        return {
            "request_id": request_id,
            "status":     "ok",
            "source":     label,
            "latency_ms": round((time.monotonic() - start) * 1000),
            "reply":      reply[:60],
            "hedged":     False,
        }

    # primary is slow — try hedge if budget allows
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        primary_task.cancel()
        return {"request_id": request_id, "status": "deadline_exceeded"}

    got_slot = await hedge_budget.acquire()
    if not got_slot:
        # no hedge budget — wait for primary
        try:
            label, reply = await asyncio.wait_for(primary_task, timeout=remaining)
            return {
                "request_id": request_id,
                "status":     "ok",
                "source":     "primary_no_hedge",
                "latency_ms": round((time.monotonic() - start) * 1000),
                "reply":      reply[:60],
                "hedged":     False,
            }
        except asyncio.TimeoutError:
            primary_task.cancel()
            return {"request_id": request_id, "status": "deadline_exceeded"}

    hedge_task = asyncio.create_task(single_call(prompt, "hedge"))
    try:
        done2, pending = await asyncio.wait(
            {primary_task, hedge_task},
            timeout=remaining,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        if not done2:
            return {"request_id": request_id, "status": "deadline_exceeded"}

        label, reply = next(iter(done2)).result()
        return {
            "request_id": request_id,
            "status":     "ok",
            "source":     label,
            "latency_ms": round((time.monotonic() - start) * 1000),
            "reply":      reply[:60],
            "hedged":     True,
        }
    finally:
        await hedge_budget.release()


async def main() -> None:
    tasks = [hedged_call(i, f"Explain concept {i % 5} briefly.") for i in range(12)]
    results = await asyncio.gather(*tasks)
    hedged  = sum(1 for r in results if r.get("hedged"))
    ok      = sum(1 for r in results if r["status"] == "ok")
    avg_lat = sum(r.get("latency_ms", 0) for r in results if r["status"] == "ok") / max(ok, 1)

    for r in results:
        src = r.get("source", "-")
        lat = r.get("latency_ms", 0)
        print(f"  [{r['request_id']:02d}] {r['status']} src={src} latency={lat}ms")

    print(f"\nOK={ok}, Hedged={hedged}, AvgLatency={avg_lat:.0f}ms")


if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Hedging uses 2x tokens on slow requests but reduces p99 latency by 40–60%
# Environment: ANTHROPIC_API_KEY must be set
```

---

## Comparison

| Option | Strategy | Storm Prevention | Priority Support | Metrics | Best For |
|--------|----------|-----------------|-----------------|---------|----------|
| 1 | Global token bucket | High | No | No | Simple single-service agents |
| 2 | Per-error-class budget | High | No | Partial | Mixed error environments |
| 3 | Admission control + jitter | High | Yes | No | Multi-priority workloads |
| 4 | Storm detector + backpressure | Highest | No | Rate gauge | Proactive auto-throttling |
| 5 | Prometheus-style metrics | High | No | Full | Production observability |
| 6 | Hedging + deadline budget | Medium | Implicit | Latency | Latency-sensitive SLOs |
