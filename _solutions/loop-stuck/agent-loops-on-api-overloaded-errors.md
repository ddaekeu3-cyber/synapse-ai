---
layout: solution
title: "Agent Loops on API Overloaded Errors"
category: loop-stuck
description: "Agent receives a 529 Overloaded error and immediately retries in a tight loop without backoff, amplifying the load on an already overloaded API and burning through rate limit budget while making no forward progress."
tags: [loop-stuck, rate-limit, overloaded, backoff, retry, resilience]
---

## Symptom

During an Anthropic API capacity event, the agent receives `{"type": "overloaded_error", "message": "Overloaded"}` on every call. Instead of backing off, it retries immediately — hundreds of times per minute. The logs show a wall of 529 errors. The agent makes no progress, burns through the per-minute rate limit budget, and when capacity recovers it cannot serve real requests because its budget is exhausted. The retry loop runs for the full timeout duration before finally failing.

## Root Cause

Retry logic written for transient errors (network blips, 500s) typically retries immediately or with a fixed short delay. A 529 Overloaded is fundamentally different: retrying immediately makes the overload worse — both for this agent and for all other clients sharing the API. Correct handling requires: (1) exponential backoff with jitter to spread retry load over time, (2) distinguishing 529 from other retriable errors, (3) a circuit breaker to stop retrying entirely if overload persists, and (4) propagating the error to the user rather than looping silently.

## Fix

### Option 1 — Exponential backoff with jitter for 529 errors

```python
import time
import random
import anthropic

client = anthropic.Anthropic()

def ask_with_backoff(
    question: str,
    max_retries: int = 6,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> str:
    """
    Retry with exponential backoff + full jitter on overloaded errors.
    Full jitter: actual_delay = random(0, min(cap, base * 2^attempt))
    This prevents thundering-herd when many clients retry simultaneously.
    """
    for attempt in range(max_retries + 1):
        try:
            r = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": question}],
            )
            return r.content[0].text

        except anthropic.InternalServerError as e:
            # 529 Overloaded surfaces as InternalServerError or APIStatusError
            is_overloaded = "overloaded" in str(e).lower() or getattr(e, "status_code", 0) == 529
            if not is_overloaded or attempt == max_retries:
                raise

            # Exponential backoff with full jitter
            cap   = min(max_delay, base_delay * (2 ** attempt))
            delay = random.uniform(0, cap)
            print(f"  [529 attempt {attempt+1}/{max_retries}] overloaded — waiting {delay:.1f}s")
            time.sleep(delay)

        except anthropic.RateLimitError as e:
            # 429 — respect Retry-After header if present
            retry_after = float(getattr(e, "response", None) and
                                e.response.headers.get("retry-after", 0) or 5)
            if attempt == max_retries:
                raise
            delay = retry_after + random.uniform(0, 2)
            print(f"  [429 attempt {attempt+1}/{max_retries}] rate limited — waiting {delay:.1f}s")
            time.sleep(delay)

        except (anthropic.APIConnectionError, anthropic.APITimeoutError) as e:
            if attempt == max_retries:
                raise
            delay = random.uniform(base_delay, base_delay * (2 ** attempt))
            print(f"  [net attempt {attempt+1}/{max_retries}] {type(e).__name__} — waiting {delay:.1f}s")
            time.sleep(delay)

    raise RuntimeError("max retries exceeded")

try:
    r = ask_with_backoff("What is the capital of France?")
    print(f"A: {r.strip()[:100]}")
except anthropic.InternalServerError:
    print("API overloaded — try again later.")
```

**Expected Token Savings:** No token reduction, but correct backoff prevents burning the per-minute rate limit budget on failed 529 retries; full jitter distributes retry load evenly, reducing the duration of API overload events for all clients.
**Environment:** All agents; exponential backoff with full jitter is the AWS-recommended standard for all retry logic and should be the baseline retry implementation.

---

### Option 2 — Circuit breaker: stop retrying after consecutive overload failures

```python
import time
import enum
import threading
import anthropic

client = anthropic.Anthropic()

class CBState(enum.Enum):
    CLOSED   = "closed"    # normal — requests pass through
    OPEN     = "open"      # tripped — all requests rejected immediately
    HALF_OPEN= "half_open" # testing — allow one probe request

class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int   = 5,
        recovery_timeout:  float = 60.0,
        probe_timeout:     float = 10.0,
    ):
        self._state             = CBState.CLOSED
        self._failures          = 0
        self._last_failure_time = 0.0
        self._failure_threshold = failure_threshold
        self._recovery_timeout  = recovery_timeout
        self._probe_timeout     = probe_timeout
        self._lock              = threading.Lock()

    @property
    def state(self) -> CBState:
        with self._lock:
            if self._state == CBState.OPEN:
                if time.monotonic() - self._last_failure_time >= self._recovery_timeout:
                    self._state = CBState.HALF_OPEN
                    print(f"  [CB] → HALF_OPEN (probing)")
            return self._state

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state    = CBState.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failures         += 1
            self._last_failure_time = time.monotonic()
            if self._failures >= self._failure_threshold:
                self._state = CBState.OPEN
                print(f"  [CB] → OPEN after {self._failures} failures")

_CB = CircuitBreaker(failure_threshold=3, recovery_timeout=30.0)

def ask(question: str) -> str:
    state = _CB.state
    if state == CBState.OPEN:
        raise RuntimeError("Circuit breaker OPEN — API is overloaded, try again later.")

    try:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": question}],
        )
        _CB.record_success()
        return r.content[0].text

    except anthropic.InternalServerError as e:
        if "overloaded" in str(e).lower() or getattr(e, "status_code", 0) == 529:
            _CB.record_failure()
            raise RuntimeError(f"API overloaded (CB failures={_CB._failures})") from e
        raise

questions = [
    "What is Python?",
    "What is asyncio?",
    "What is a decorator?",
]
for q in questions:
    try:
        print(f"Q: {q} [CB state: {_CB.state.value}]")
        r = ask(q)
        print(f"A: {r.strip()[:80]}\n")
    except RuntimeError as e:
        print(f"  BLOCKED: {e}\n")
```

**Expected Token Savings:** Circuit breaker stops all retry attempts immediately once the failure threshold is reached — preventing N×retry_budget tokens from being spent on calls that have no chance of succeeding during an overload event.
**Environment:** Production agents with strict rate limit budgets; the circuit breaker is the most important resilience pattern for API overload scenarios where retrying amplifies the problem.

---

### Option 3 — Async retry with semaphore: limit concurrent retry attempts

```python
import asyncio
import random
import anthropic

client = anthropic.AsyncAnthropic()

# Limit concurrent requests to avoid amplifying overload
_SEM = asyncio.Semaphore(5)

async def ask_with_async_backoff(
    question: str,
    max_retries: int   = 5,
    base_delay:  float = 2.0,
) -> str:
    async with _SEM:
        for attempt in range(max_retries + 1):
            try:
                r = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=128,
                    messages=[{"role": "user", "content": question}],
                )
                return r.content[0].text

            except anthropic.InternalServerError as e:
                is_overloaded = "overloaded" in str(e).lower()
                if not is_overloaded or attempt == max_retries:
                    raise
                delay = min(60.0, base_delay * (2 ** attempt)) * (0.5 + random.random() * 0.5)
                print(f"  [529/{attempt+1}] '{question[:30]}...' → wait {delay:.1f}s")
                await asyncio.sleep(delay)

            except anthropic.RateLimitError:
                if attempt == max_retries:
                    raise
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                await asyncio.sleep(delay)

    raise RuntimeError("max retries exceeded")

async def main() -> None:
    import time
    questions = [f"Name a {animal} in one word." for animal in
                 ["mammal", "bird", "reptile", "fish", "insect"]]

    t0      = time.perf_counter()
    results = await asyncio.gather(
        *[ask_with_async_backoff(q) for q in questions],
        return_exceptions=True,
    )
    elapsed = time.perf_counter() - t0
    print(f"\n{len(questions)} async calls in {elapsed:.1f}s")
    for q, r in zip(questions, results):
        if isinstance(r, Exception):
            print(f"  FAILED: {q} — {r}")
        else:
            print(f"  OK: {q} → {r.strip()[:30]}")

asyncio.run(main())
```

**Expected Token Savings:** Semaphore-bounded async retry prevents a burst of 100 concurrent requests from all retrying simultaneously after a 529 — only 5 probe calls go out at a time, proportionally reducing retry amplification.
**Environment:** Async agents with high concurrency; the semaphore is essential during overload events where every concurrent retry makes the overload worse.

---

### Option 4 — Overload budget: track and enforce a per-minute 529 budget

```python
import time
import collections
import anthropic

client = anthropic.Anthropic()

class OverloadBudget:
    """
    Tracks 529 errors in a sliding window.
    Blocks new requests when the error rate exceeds the threshold.
    """

    def __init__(self, window_s: float = 60.0, max_errors: int = 10) -> None:
        self._window     = window_s
        self._max_errors = max_errors
        self._errors:    collections.deque = collections.deque()

    def record_error(self) -> None:
        self._errors.append(time.monotonic())
        self._purge()

    def _purge(self) -> None:
        cutoff = time.monotonic() - self._window
        while self._errors and self._errors[0] < cutoff:
            self._errors.popleft()

    @property
    def error_count(self) -> int:
        self._purge()
        return len(self._errors)

    @property
    def is_exhausted(self) -> bool:
        return self.error_count >= self._max_errors

    def wait_time(self) -> float:
        """How long to wait before the oldest error leaves the window."""
        self._purge()
        if not self._errors or not self.is_exhausted:
            return 0.0
        oldest = self._errors[0]
        return max(0.0, (oldest + self._window) - time.monotonic())

_BUDGET = OverloadBudget(window_s=60.0, max_errors=5)

def ask(question: str) -> str:
    # Check budget before making the call
    if _BUDGET.is_exhausted:
        wait = _BUDGET.wait_time()
        print(f"  [budget] {_BUDGET.error_count} overload errors in 60s — pause {wait:.0f}s")
        time.sleep(wait)

    try:
        r = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": question}],
        )
        return r.content[0].text
    except anthropic.InternalServerError as e:
        if "overloaded" in str(e).lower():
            _BUDGET.record_error()
            print(f"  [budget] overload recorded ({_BUDGET.error_count}/{_BUDGET._max_errors})")
        raise

questions = ["What is REST?", "What is GraphQL?", "What is gRPC?"]
for q in questions:
    try:
        r = ask(q)
        print(f"Q: {q}\nA: {r.strip()[:80]}\n")
    except anthropic.InternalServerError:
        print(f"  FAILED: {q}\n")
```

**Expected Token Savings:** Overload budget enforces a maximum retry rate at the application level — after 5 failures in 60 seconds, the agent pauses until the window clears instead of continuing to fire failing requests; this converts an unbounded retry loop into a bounded, self-throttling system.
**Environment:** High-concurrency production agents; overload budget tracking is the preventive complement to circuit breakers — the budget fires before the circuit trips.

---

### Option 5 — Fallback to a secondary endpoint or degraded mode on overload

```python
import time
import random
import anthropic

primary_client = anthropic.Anthropic()

FALLBACK_SYSTEM = "Answer very briefly — one sentence maximum."

def ask_with_fallback(question: str, max_retries: int = 3) -> tuple[str, str]:
    """
    Primary: full-quality answer with claude-haiku-4-5-20251001.
    Fallback: shorter answer from claude-haiku-4-5-20251001 with reduced max_tokens.
    Returns (answer, mode).
    """
    # Attempt primary with backoff
    for attempt in range(max_retries):
        try:
            r = primary_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=256,
                messages=[{"role": "user", "content": question}],
            )
            return r.content[0].text, "primary"
        except anthropic.InternalServerError as e:
            if "overloaded" not in str(e).lower() or attempt == max_retries - 1:
                break
            delay = (2 ** attempt) * random.uniform(0.5, 1.5)
            print(f"  [primary 529 attempt {attempt+1}] waiting {delay:.1f}s")
            time.sleep(delay)
        except Exception:
            break   # non-overload error — fall through to fallback

    # Fallback: degraded mode (shorter, cheaper call)
    print("  [fallback] switching to degraded mode")
    try:
        r = primary_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            system=FALLBACK_SYSTEM,
            messages=[{"role": "user", "content": question}],
        )
        return r.content[0].text, "degraded"
    except anthropic.InternalServerError:
        return "Service temporarily unavailable. Please try again in a few minutes.", "unavailable"

questions = [
    "Explain how TCP three-way handshake works.",
    "What is the difference between authentication and authorisation?",
]
for q in questions:
    answer, mode = ask_with_fallback(q)
    print(f"Q: {q}")
    print(f"A [{mode}]: {answer.strip()[:200]}\n")
```

**Expected Token Savings:** Fallback to a degraded mode serves a useful (if shorter) answer during overload instead of failing entirely — preserving user experience and reducing the repeated-retry cost; a 64-token degraded answer costs 75% less than a 256-token primary answer.
**Environment:** User-facing agents where graceful degradation is preferable to hard failure; fallback mode is the production-facing complement to circuit breakers, ensuring the agent always returns something useful.

---

### Option 6 — Overload detector: proactive health probe before batch processing

```python
import time
import asyncio
import anthropic

client = anthropic.AsyncAnthropic()

async def probe_api_health(timeout: float = 3.0) -> dict:
    """Send a minimal probe call to detect overload before starting a batch."""
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(
            client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            ),
            timeout=timeout,
        )
        latency_ms = (time.monotonic() - t0) * 1000
        return {"healthy": True, "latency_ms": round(latency_ms)}
    except asyncio.TimeoutError:
        return {"healthy": False, "reason": "timeout", "latency_ms": timeout * 1000}
    except anthropic.InternalServerError as e:
        overloaded = "overloaded" in str(e).lower()
        return {"healthy": False, "reason": "overloaded" if overloaded else str(e)}
    except Exception as e:
        return {"healthy": False, "reason": str(type(e).__name__)}

async def run_batch_with_health_check(items: list[str], max_wait_s: float = 120.0) -> list[str]:
    """Probe API health before and during batch; pause if overloaded."""
    deadline = time.monotonic() + max_wait_s

    # Pre-batch health check
    health = await probe_api_health()
    if not health["healthy"]:
        reason = health["reason"]
        if reason == "overloaded":
            print(f"  [health] API overloaded — waiting for capacity...")
            while time.monotonic() < deadline:
                await asyncio.sleep(15)
                health = await probe_api_health()
                print(f"  [health] {health}")
                if health["healthy"]:
                    break
            else:
                raise TimeoutError("API remained overloaded — batch cancelled")
        else:
            raise RuntimeError(f"API unhealthy: {reason}")

    print(f"  [health] API healthy (latency={health.get('latency_ms')}ms) — starting batch")

    # Process batch with inline health checks every 10 items
    results = []
    for i, item in enumerate(items):
        if i > 0 and i % 10 == 0:
            health = await probe_api_health()
            if not health["healthy"]:
                print(f"  [health] overloaded mid-batch at item {i} — pausing 30s")
                await asyncio.sleep(30)

        r = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=32,
            messages=[{"role": "user", "content": item}],
        )
        results.append(r.content[0].text.strip())

    return results

async def main() -> None:
    items = [f"Name one {topic}." for topic in
             ["mammal", "bird", "fish", "insect", "reptile"]]
    results = await run_batch_with_health_check(items)
    for item, result in zip(items, results):
        print(f"  {item}: {result[:40]}")

asyncio.run(main())
```

**Expected Token Savings:** Pre-batch probing catches overload before committing a large batch — without it, a 1,000-item batch might send 200 requests before detecting overload, burning 200 × request_tokens; probing costs 1 token and saves potentially thousands of failed request tokens.
**Environment:** Batch processing agents that periodically run large jobs; health probing before batches prevents the catastrophic scenario of processing 10% of a batch before hitting sustained overload.

---

## Comparison

| Option | Prevents Retry Storm | Fails Fast | User Gets Response | Best For |
|---|---|---|---|---|
| 1. Exponential backoff + jitter | Yes (spaced retries) | No (retries N times) | Eventually | All agents — baseline pattern |
| 2. Circuit breaker | Yes (stops all retries) | Yes (OPEN state) | Error message | High-volume production agents |
| 3. Async semaphore | Yes (limits concurrency) | No | Eventually | Concurrent async agents |
| 4. Overload budget | Yes (self-throttles) | Partial | Delayed | Per-minute budget enforcement |
| 5. Fallback + degraded mode | No (still retries) | No | Yes (degraded) | User-facing agents, UX resilience |
| 6. Pre-batch health probe | Yes (delays batch start) | Yes (cancels batch) | Delayed | Scheduled batch processing agents |
