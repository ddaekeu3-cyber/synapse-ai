---
layout: solution
title: "Agent Doesn't Implement Retry Storm Prevention"
category: general
description: "Multiple agent instances retrying simultaneously after a shared API failure create a 'thundering herd' — all workers hit the endpoint at the same time, overwhelming recovery. Retry storm prevention uses jitter, coordinated backoff, and circuit breakers to spread retries across time and prevent cascading overload."
tags: [general, retry, thundering-herd, jitter, backoff, circuit-breaker, rate-limit, concurrency]
---

## Problem

When an API becomes briefly unavailable and 20 agent workers all retry at the same second, they create a second outage even after the API recovers. Simple exponential backoff without jitter synchronizes retries because all workers share the same interval schedule. Proper retry storm prevention adds random jitter to desynchronize workers, uses coordinated rate gates to limit simultaneous retries, and implements circuit breakers that open when failure rates spike — giving the downstream service time to recover.

## Solutions

### Option 1: Full Jitter Exponential Backoff

```python
import anthropic
import asyncio
import random
import time

client = anthropic.AsyncAnthropic()

async def call_with_full_jitter(
    prompt: str,
    max_retries: int = 5,
    base_delay: float = 1.0,
    cap: float = 32.0,
) -> str:
    """
    Full jitter: delay = random(0, min(cap, base * 2^attempt))
    This is the AWS-recommended approach for preventing thundering herds.
    """
    for attempt in range(max_retries + 1):
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text

        except anthropic.RateLimitError:
            if attempt >= max_retries:
                raise
            # Full jitter: [0, exponential_cap)
            window = min(cap, base_delay * (2 ** attempt))
            delay = random.uniform(0, window)
            print(f"  [retry {attempt+1}] RateLimitError — waiting {delay:.2f}s (full jitter, window={window:.1f}s)")
            await asyncio.sleep(delay)

        except anthropic.APIStatusError as e:
            if e.status_code < 500 or attempt >= max_retries:
                raise
            window = min(cap, base_delay * (2 ** attempt))
            delay = random.uniform(0, window)
            print(f"  [retry {attempt+1}] {e.status_code} — waiting {delay:.2f}s")
            await asyncio.sleep(delay)

async def show_desynchronization():
    """Demonstrate that jitter desynchronizes workers."""
    print("Without jitter (synchronized retries):")
    no_jitter = [1.0 * (2 ** i) for i in range(4)]
    print(f"  Delays: {no_jitter}")

    print("\nWith full jitter (desynchronized):")
    for worker in range(4):
        delays = [random.uniform(0, min(32.0, 1.0 * (2 ** i))) for i in range(4)]
        print(f"  Worker {worker}: {[round(d, 2) for d in delays]}")

if __name__ == "__main__":
    asyncio.run(show_desynchronization())

    # Actual call
    result = asyncio.run(call_with_full_jitter("What is 2 + 2?"))
    print(f"\nResult: {result.strip()}")

# Expected Token Savings: jitter prevents synchronized retries that would hit API while recovering
# Environment: any multi-worker agent deployment; full jitter is superior to decorrelated jitter for thundering herd
```

### Option 2: Coordinated Retry Gate — Shared Rate Limit Across Workers

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

class CoordinatedRetryGate:
    """
    Shared gate that limits the total retry rate across all workers.
    When many workers want to retry, only N retries per window are allowed.
    The rest wait, preventing simultaneous retry storms.
    """
    def __init__(self, max_retries_per_window: int = 3, window_seconds: float = 10.0):
        self._max = max_retries_per_window
        self._window = window_seconds
        self._timestamps: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire_retry_slot(self) -> float:
        """Wait until a retry slot is available. Returns wait time."""
        while True:
            async with self._lock:
                now = time.time()
                cutoff = now - self._window
                self._timestamps = [t for t in self._timestamps if t >= cutoff]
                if len(self._timestamps) < self._max:
                    self._timestamps.append(now)
                    return 0.0
                wait = self._timestamps[0] + self._window - now
            print(f"  [gate] retry slot full, waiting {wait:.1f}s")
            await asyncio.sleep(max(0.1, wait))

_gate = CoordinatedRetryGate(max_retries_per_window=3, window_seconds=5.0)

async def call_with_gate(worker_id: int, prompt: str, max_retries: int = 3) -> str:
    import random
    for attempt in range(max_retries + 1):
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text

        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            if attempt >= max_retries:
                raise
            # Acquire a shared retry slot before retrying
            wait = await _gate.acquire_retry_slot()
            jitter = random.uniform(0, 0.5)
            total_wait = wait + jitter
            print(f"  [W{worker_id}] attempt {attempt+1} — gate delay={total_wait:.2f}s")
            await asyncio.sleep(total_wait)

async def main():
    # Simulate 8 workers all hitting an error at the same time
    tasks = [
        call_with_gate(i, f"Worker {i}: what is {i} + {i}?")
        for i in range(6)
    ]
    t0 = time.time()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.time() - t0
    print(f"\nCompleted {len([r for r in results if not isinstance(r, Exception)])} calls in {elapsed:.1f}s")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: coordinated gate prevents N workers all retrying at once after a shared failure
# Environment: multi-worker agents sharing the same Anthropic API key; gate is process-level coordination
```

### Option 3: Circuit Breaker with Half-Open Probe

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass
from enum import Enum

client = anthropic.AsyncAnthropic()

class CircuitState(Enum):
    CLOSED = "closed"       # Normal operation
    OPEN = "open"           # Blocking all requests
    HALF_OPEN = "half_open" # Testing recovery

@dataclass
class CircuitBreaker:
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    probe_interval: float = 5.0

    def __post_init__(self):
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: float | None = None
        self._last_probe: float = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        return self._state

    async def call(self, fn, *args, **kwargs):
        async with self._lock:
            now = time.time()

            if self._state == CircuitState.OPEN:
                if now - self._opened_at >= self._probe_interval:
                    self._state = CircuitState.HALF_OPEN
                    print(f"  [circuit] OPEN → HALF_OPEN (probing)")
                else:
                    wait = self._probe_interval - (now - self._opened_at)
                    raise RuntimeError(f"Circuit OPEN — retry in {wait:.1f}s")

        try:
            result = await fn(*args, **kwargs)
            async with self._lock:
                if self._state == CircuitState.HALF_OPEN:
                    print(f"  [circuit] HALF_OPEN → CLOSED (recovered)")
                self._state = CircuitState.CLOSED
                self._failures = 0
            return result

        except Exception as e:
            async with self._lock:
                self._failures += 1
                if self._state == CircuitState.HALF_OPEN:
                    self._state = CircuitState.OPEN
                    self._opened_at = time.time()
                    print(f"  [circuit] HALF_OPEN → OPEN (probe failed)")
                elif self._failures >= self.failure_threshold:
                    self._state = CircuitState.OPEN
                    self._opened_at = time.time()
                    print(f"  [circuit] CLOSED → OPEN ({self._failures} failures)")
            raise

breaker = CircuitBreaker(failure_threshold=3, probe_interval=2.0)

async def claude_call(prompt: str) -> str:
    async def _call():
        resp = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    return await breaker.call(_call)

async def main():
    prompts = [f"What is {i} + {i}?" for i in range(6)]
    for i, p in enumerate(prompts):
        try:
            result = await claude_call(p)
            print(f"  [{i+1}] {breaker.state.value:9s} | OK: {result.strip()[:30]}")
        except RuntimeError as e:
            print(f"  [{i+1}] {breaker.state.value:9s} | BLOCKED: {e}")
        except Exception as e:
            print(f"  [{i+1}] {breaker.state.value:9s} | ERROR: {type(e).__name__}")
        await asyncio.sleep(0.3)

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: open circuit blocks 100% of retries during outage — prevents spending tokens on doomed calls
# Environment: production agents; circuit breaker prevents cascading failures when Anthropic API is degraded
```

### Option 4: Retry Budget — Global Cap on Total Retry Tokens Spent

```python
import anthropic
import asyncio
import random
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class RetryBudget:
    """
    Global retry budget: tracks total retries across all workers.
    When budget is exhausted, no more retries — fail fast.
    """
    max_retries_total: int = 20
    _used: int = 0
    _lock: asyncio.Lock = None

    def __post_init__(self):
        self._lock = asyncio.Lock()

    async def consume(self) -> bool:
        """Returns True if retry is allowed, False if budget exhausted."""
        async with self._lock:
            if self._used >= self.max_retries_total:
                return False
            self._used += 1
            return True

    @property
    def remaining(self) -> int:
        return max(0, self.max_retries_total - self._used)

    @property
    def utilization(self) -> float:
        return self._used / self.max_retries_total

_budget = RetryBudget(max_retries_total=10)

async def call_with_budget(worker_id: int, prompt: str) -> str:
    for attempt in range(5):
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text

        except (anthropic.RateLimitError, anthropic.APIStatusError):
            allowed = await _budget.consume()
            if not allowed:
                raise RuntimeError(
                    f"Global retry budget exhausted ({_budget.max_retries_total} retries used)"
                )
            delay = random.uniform(0.5, 2.0 * (attempt + 1))
            print(f"  [W{worker_id}] retry {attempt+1} — delay={delay:.1f}s budget_left={_budget.remaining}")
            await asyncio.sleep(delay)

async def main():
    tasks = [
        call_with_budget(i, f"Say 'result {i}' only.")
        for i in range(8)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    successes = sum(1 for r in results if isinstance(r, str))
    failures = sum(1 for r in results if isinstance(r, Exception))
    print(f"\nSuccesses: {successes} | Failures: {failures} | Budget used: {_budget._used}/{_budget.max_retries_total}")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: retry budget prevents tail workers from exhausting quota on hopeless retries
# Environment: fan-out agents where some calls can fail without failing the entire batch
```

### Option 5: Retry-After Header Respect with Coordinated Wake

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

_earliest_retry: float = 0.0
_retry_lock = asyncio.Lock()

async def update_retry_time(retry_after_seconds: float):
    """Record the earliest time any worker should retry, based on Retry-After header."""
    global _earliest_retry
    async with _retry_lock:
        earliest = time.time() + retry_after_seconds
        if earliest > _earliest_retry:
            _earliest_retry = earliest
            print(f"  [global] Retry-After: hold until {retry_after_seconds:.0f}s from now")

async def wait_for_retry_window():
    """Wait until the global retry window has passed."""
    async with _retry_lock:
        wait = _earliest_retry - time.time()
    if wait > 0:
        print(f"  [wait] respecting Retry-After — sleeping {wait:.1f}s")
        await asyncio.sleep(wait + 0.1)  # small buffer

async def call_respecting_retry_after(worker_id: int, prompt: str, max_retries: int = 3) -> str:
    import random
    for attempt in range(max_retries + 1):
        # Check global retry window before each call
        await wait_for_retry_window()

        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text

        except anthropic.RateLimitError as e:
            if attempt >= max_retries:
                raise
            # Extract Retry-After from headers if available
            retry_after = 5.0  # default
            if hasattr(e, "response") and e.response:
                header_val = e.response.headers.get("retry-after", "5")
                try:
                    retry_after = float(header_val)
                except ValueError:
                    pass
            await update_retry_time(retry_after)
            jitter = random.uniform(0, 1.0)
            print(f"  [W{worker_id}] 429 — coordinated hold + {jitter:.2f}s jitter")

async def main():
    tasks = [
        call_respecting_retry_after(i, f"Count to {i+1}.")
        for i in range(5)
    ]
    t0 = time.time()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    elapsed = time.time() - t0
    ok = sum(1 for r in results if isinstance(r, str))
    print(f"\nCompleted {ok}/{len(results)} in {elapsed:.1f}s")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: Retry-After respected globally — no worker wastes tokens before the API is ready
# Environment: rate-limited deployments; global coordination prevents any worker from retrying too early
```

### Option 6: Adaptive Retry Rate Based on Error Frequency

```python
import anthropic
import asyncio
import time
from collections import deque

client = anthropic.AsyncAnthropic()

class AdaptiveRetryController:
    """
    Monitors error rate over a sliding window and adjusts retry delay dynamically.
    High error rate → increase delays. Low error rate → decrease delays.
    """
    def __init__(self, window_seconds: float = 30.0, target_error_rate: float = 0.1):
        self._window = window_seconds
        self._target_rate = target_error_rate
        self._events: deque[tuple[float, bool]] = deque()  # (timestamp, is_error)
        self._lock = asyncio.Lock()
        self._base_delay = 1.0

    def _evict_old(self, now: float):
        cutoff = now - self._window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

    async def record(self, success: bool):
        async with self._lock:
            self._events.append((time.time(), not success))

    async def get_delay(self, attempt: int) -> float:
        """Calculate delay based on current error rate."""
        async with self._lock:
            now = time.time()
            self._evict_old(now)
            if not self._events:
                error_rate = 0.0
            else:
                errors = sum(1 for _, is_err in self._events if is_err)
                error_rate = errors / len(self._events)

            # Scale delay by how much error rate exceeds target
            if error_rate <= self._target_rate:
                multiplier = 1.0
            else:
                multiplier = min(8.0, error_rate / self._target_rate)

            base = self._base_delay * (2 ** attempt) * multiplier
            import random
            delay = random.uniform(0, min(30.0, base))
            return delay, error_rate

controller = AdaptiveRetryController()

async def adaptive_call(worker_id: int, prompt: str, max_retries: int = 3) -> str:
    for attempt in range(max_retries + 1):
        try:
            resp = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=32,
                messages=[{"role": "user", "content": prompt}],
            )
            await controller.record(success=True)
            return resp.content[0].text

        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            await controller.record(success=False)
            if attempt >= max_retries:
                raise
            delay, error_rate = await controller.get_delay(attempt)
            print(f"  [W{worker_id}] error_rate={error_rate:.0%} → delay={delay:.2f}s")
            await asyncio.sleep(delay)

async def main():
    tasks = [
        adaptive_call(i, f"What is {i}*{i}?")
        for i in range(6)
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = sum(1 for r in results if isinstance(r, str))
    print(f"\nResults: {ok}/{len(results)} succeeded")

if __name__ == "__main__":
    asyncio.run(main())

# Expected Token Savings: during high error rates, longer delays prevent wasting tokens on likely-to-fail calls
# Environment: agents with variable load; adaptive delays back off aggressively during incidents, recover smoothly
```

## Comparison

| Option | Jitter Type | Coordination | Circuit Break | Persistence |
|--------|-----------|-------------|--------------|-------------|
| 1 — Full jitter | Full random | Per-worker | No | No |
| 2 — Coordinated gate | Jitter + gate | Shared semaphore | No | No |
| 3 — Circuit breaker | None | Circuit state | Yes | No |
| 4 — Retry budget | Jitter | Global counter | No | No |
| 5 — Retry-After respect | Jitter | Global wake time | No | No |
| 6 — Adaptive rate | Jitter | Error-rate-scaled | No | No |
