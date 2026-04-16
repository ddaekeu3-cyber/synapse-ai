---
layout: solution
title: "Agent Doesn't Implement Exponential Backoff with Jitter for API Retries"
category: reliability
description: "Agents that retry immediately on failure cause thundering-herd problems and get banned by rate limiters. These patterns show how to implement exponential backoff with jitter so retries are safe, respectful, and effective."
tags: [reliability, retry, backoff, jitter, rate-limit, anthropic]
---

## Problem

An agent that retries a failed API call immediately — or at fixed intervals — amplifies the very problem it tries to solve. A 429 rate-limit error followed by ten instant retries generates ten more 429s. Exponential backoff with jitter spreads retries across time so bursts dissipate, servers recover, and agents self-heal without worsening load.

---

### Option 1: Basic Exponential Backoff with Full Jitter

Add random jitter across the full backoff window — the most effective retry strategy against synchronized retry storms.

```python
import time
import random
import anthropic
from anthropic import RateLimitError, APIStatusError, APIConnectionError

client = anthropic.Anthropic()

def exponential_backoff_with_jitter(
    attempt: int,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    multiplier: float = 2.0,
) -> float:
    """Full jitter: random value in [0, min(max_delay, base * 2^attempt)]."""
    cap = min(max_delay, base_delay * (multiplier ** attempt))
    return random.uniform(0, cap)

def call_with_retry(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
    max_attempts: int = 5,
) -> str:
    last_error = None
    for attempt in range(max_attempts):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        except RateLimitError as e:
            delay = exponential_backoff_with_jitter(attempt, base_delay=2.0, max_delay=60.0)
            print(f"[rate limit: attempt {attempt+1}/{max_attempts}, sleeping {delay:.1f}s]")
            last_error = e

        except APIConnectionError as e:
            delay = exponential_backoff_with_jitter(attempt, base_delay=1.0, max_delay=30.0)
            print(f"[connection error: attempt {attempt+1}/{max_attempts}, sleeping {delay:.1f}s]")
            last_error = e

        except APIStatusError as e:
            if e.status_code in (500, 502, 503, 529):
                delay = exponential_backoff_with_jitter(attempt, base_delay=5.0, max_delay=120.0)
                print(f"[server error {e.status_code}: attempt {attempt+1}/{max_attempts}, sleeping {delay:.1f}s]")
                last_error = e
            else:
                raise   # 4xx client errors don't retry

        if attempt < max_attempts - 1:
            time.sleep(exponential_backoff_with_jitter(attempt))

    raise RuntimeError(f"Failed after {max_attempts} attempts: {last_error}")

if __name__ == "__main__":
    result = call_with_retry("What is the capital of France?", max_attempts=3)
    print(result)

# Expected Token Savings: Jitter prevents retry storms that waste tokens on redundant 429-error responses
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Retry with Respect for Retry-After Headers

Parse the `retry-after` header from rate-limit responses and honor it before falling back to backoff.

```python
import time
import random
import anthropic
from anthropic import RateLimitError, APIStatusError

client = anthropic.Anthropic()

def parse_retry_after(error: Exception) -> float | None:
    """Extract Retry-After value from error headers if present."""
    headers = getattr(getattr(error, "response", None), "headers", {})
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw:
        try:
            return float(raw)
        except (ValueError, TypeError):
            pass
    return None

def get_delay(attempt: int, error: Exception, base: float = 1.0, max_delay: float = 120.0) -> float:
    # Honor server-provided retry-after if present
    server_delay = parse_retry_after(error)
    if server_delay is not None:
        jitter = random.uniform(0, min(2.0, server_delay * 0.1))
        delay = server_delay + jitter
        print(f"  [using server retry-after: {server_delay}s + {jitter:.2f}s jitter]")
        return delay

    # Fall back to exponential backoff with full jitter
    cap = min(max_delay, base * (2 ** attempt))
    return random.uniform(0, cap)

def retry_respecting_headers(
    prompt: str,
    max_attempts: int = 5,
    model: str = "claude-sonnet-4-6",
) -> str:
    for attempt in range(max_attempts):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            if attempt > 0:
                print(f"[succeeded on attempt {attempt+1}]")
            return response.content[0].text

        except (RateLimitError, APIStatusError) as e:
            if isinstance(e, APIStatusError) and e.status_code not in (429, 500, 502, 503, 529):
                raise
            delay = get_delay(attempt, e)
            print(f"[retry {attempt+1}/{max_attempts}: sleeping {delay:.2f}s]")
            if attempt < max_attempts - 1:
                time.sleep(delay)

    raise RuntimeError(f"Exhausted {max_attempts} attempts")

if __name__ == "__main__":
    result = retry_respecting_headers("Explain eventual consistency.", max_attempts=4)
    print(result[:300])

# Expected Token Savings: Honoring retry-after prevents wasted calls during server-specified cooldown periods
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Async Retry with Decorrelated Jitter

Use decorrelated jitter (AWS-recommended) which produces better backoff distributions than full jitter for high-concurrency scenarios.

```python
import asyncio
import random
import anthropic
from anthropic import RateLimitError, APIStatusError, APIConnectionError

client = anthropic.AsyncAnthropic()

def decorrelated_jitter(attempt: int, prev_sleep: float,
                         base: float = 1.0, max_delay: float = 60.0) -> float:
    """Decorrelated jitter: sleep = random(base, prev_sleep * 3), capped at max_delay."""
    return min(max_delay, random.uniform(base, prev_sleep * 3))

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 529}

async def async_call_with_retry(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
    max_attempts: int = 6,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> str:
    sleep = base_delay
    for attempt in range(max_attempts):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        except RateLimitError:
            sleep = decorrelated_jitter(attempt, sleep, base_delay, max_delay)
            print(f"[rate limit: attempt {attempt+1}, sleep={sleep:.2f}s]")

        except APIConnectionError:
            sleep = decorrelated_jitter(attempt, sleep, base_delay / 2, max_delay / 2)
            print(f"[conn error: attempt {attempt+1}, sleep={sleep:.2f}s]")

        except APIStatusError as e:
            if e.status_code not in RETRYABLE_STATUS_CODES:
                raise
            sleep = decorrelated_jitter(attempt, sleep, base_delay * 2, max_delay)
            print(f"[status {e.status_code}: attempt {attempt+1}, sleep={sleep:.2f}s]")

        if attempt < max_attempts - 1:
            await asyncio.sleep(sleep)

    raise RuntimeError(f"Failed after {max_attempts} attempts")

async def run_parallel_with_retry(prompts: list[str]) -> list[str]:
    """Run multiple prompts concurrently, each with independent retry."""
    tasks = [async_call_with_retry(p) for p in prompts]
    return await asyncio.gather(*tasks, return_exceptions=True)

if __name__ == "__main__":
    async def main():
        prompts = [
            "What is a hash map?",
            "Explain TCP handshake.",
            "What is CAP theorem?",
        ]
        results = await run_parallel_with_retry(prompts)
        for p, r in zip(prompts, results):
            print(f"Q: {p}")
            if isinstance(r, Exception):
                print(f"  ERROR: {r}")
            else:
                print(f"  A: {r[:100]}")
    asyncio.run(main())

# Expected Token Savings: Decorrelated jitter further reduces retry clustering vs full jitter in high-concurrency
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Retry Budget with Per-Error-Class Limits

Enforce a total retry budget so misbehaving callers can't consume infinite retries on persistent failures.

```python
import asyncio
import random
import time
from dataclasses import dataclass, field
from collections import defaultdict
import anthropic
from anthropic import RateLimitError, APIStatusError, APIConnectionError

client = anthropic.AsyncAnthropic()

@dataclass
class RetryBudget:
    max_total_retries: int = 20
    max_retries_per_error: dict = field(default_factory=lambda: {
        "rate_limit": 8,
        "server_error": 5,
        "connection": 4,
    })
    _used: dict = field(default_factory=lambda: defaultdict(int))
    _total: int = 0

    def consume(self, error_class: str) -> bool:
        """Returns True if retry is allowed."""
        if self._total >= self.max_total_retries:
            return False
        if self._used[error_class] >= self.max_retries_per_error.get(error_class, 3):
            return False
        self._used[error_class] += 1
        self._total += 1
        return True

    def summary(self) -> str:
        return f"total={self._total}/{self.max_total_retries}, " + \
               ", ".join(f"{k}={v}" for k, v in self._used.items())

def classify_error(e: Exception) -> str | None:
    if isinstance(e, RateLimitError):
        return "rate_limit"
    if isinstance(e, APIConnectionError):
        return "connection"
    if isinstance(e, APIStatusError) and e.status_code in (500, 502, 503, 529):
        return "server_error"
    return None  # non-retryable

async def budgeted_retry(
    prompt: str,
    budget: RetryBudget,
    model: str = "claude-sonnet-4-6",
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> str:
    attempt = 0
    while True:
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.content[0].text

        except Exception as e:
            error_class = classify_error(e)
            if error_class is None:
                raise  # non-retryable

            if not budget.consume(error_class):
                raise RuntimeError(
                    f"Retry budget exhausted ({budget.summary()}): {e}"
                )

            cap = min(max_delay, base_delay * (2 ** attempt))
            delay = random.uniform(0, cap)
            print(f"[{error_class}: attempt {attempt+1}, budget {budget.summary()}, sleep {delay:.2f}s]")
            await asyncio.sleep(delay)
            attempt += 1

async def run_with_shared_budget():
    budget = RetryBudget(max_total_retries=15)
    prompts = [
        "Explain distributed consensus algorithms.",
        "What is the Raft consensus protocol?",
        "Compare Raft vs Paxos.",
    ]
    tasks = [budgeted_retry(p, budget) for p in prompts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    print(f"\n[Final budget: {budget.summary()}]")
    for p, r in zip(prompts, results):
        status = "OK" if not isinstance(r, Exception) else f"FAIL: {r}"
        print(f"  {p[:40]}: {status}")
    return results

if __name__ == "__main__":
    asyncio.run(run_with_shared_budget())

# Expected Token Savings: Budget prevents runaway retry loops from consuming all tokens on persistent failures
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Adaptive Backoff Tuned by Observed Success Rate

Track recent success/failure rates and increase or decrease backoff dynamically.

```python
import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
import anthropic
from anthropic import RateLimitError, APIStatusError

client = anthropic.AsyncAnthropic()

@dataclass
class AdaptiveBackoff:
    window_size: int = 20
    base_delay: float = 0.5
    max_delay: float = 60.0
    _history: deque = field(default_factory=lambda: deque(maxlen=20))
    _current_multiplier: float = 1.0

    def record(self, success: bool):
        self._history.append(1 if success else 0)
        if len(self._history) >= 5:
            rate = sum(self._history) / len(self._history)
            if rate > 0.9:
                # High success: slowly reduce multiplier
                self._current_multiplier = max(1.0, self._current_multiplier * 0.8)
            elif rate < 0.5:
                # Many failures: increase multiplier
                self._current_multiplier = min(8.0, self._current_multiplier * 1.5)

    def get_delay(self, attempt: int) -> float:
        cap = min(self.max_delay, self.base_delay * self._current_multiplier * (2 ** attempt))
        return random.uniform(0, cap)

    @property
    def success_rate(self) -> float:
        if not self._history:
            return 1.0
        return sum(self._history) / len(self._history)

backoff = AdaptiveBackoff()

async def adaptive_call(prompt: str, model: str = "claude-haiku-4-5-20251001",
                         max_attempts: int = 5) -> str:
    for attempt in range(max_attempts):
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            backoff.record(True)
            return response.content[0].text

        except (RateLimitError, APIStatusError) as e:
            if isinstance(e, APIStatusError) and e.status_code not in (429, 500, 502, 503):
                raise
            backoff.record(False)
            delay = backoff.get_delay(attempt)
            print(f"[adaptive: rate={backoff.success_rate:.0%}, "
                  f"multiplier={backoff._current_multiplier:.1f}, sleep={delay:.2f}s]")
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay)

    raise RuntimeError(f"Failed after {max_attempts} attempts")

async def run_batch(prompts: list[str]) -> list[str]:
    results = []
    for p in prompts:
        try:
            r = await adaptive_call(p)
            results.append(r)
        except Exception as e:
            results.append(f"ERROR: {e}")
    print(f"\n[final success rate: {backoff.success_rate:.0%}, "
          f"multiplier: {backoff._current_multiplier:.2f}]")
    return results

if __name__ == "__main__":
    async def main():
        prompts = [f"Give me a one-sentence fact about topic #{i}" for i in range(6)]
        results = await run_batch(prompts)
        for p, r in zip(prompts, results):
            print(f"  {p}: {r[:80]}")
    asyncio.run(main())

# Expected Token Savings: Reduces backoff overhead during healthy periods; self-heals during bursts
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Retry Middleware with Prometheus Metrics

Wrap all API calls in a retry middleware that emits metrics for retry rate, latency, and error class.

```python
import time
import random
import asyncio
from dataclasses import dataclass, field
from collections import defaultdict
import anthropic
from anthropic import RateLimitError, APIStatusError, APIConnectionError

client = anthropic.AsyncAnthropic()

@dataclass
class RetryMetrics:
    """Simple Prometheus-style counter/histogram."""
    _counters: dict = field(default_factory=lambda: defaultdict(int))
    _durations: list = field(default_factory=list)

    def inc(self, name: str, labels: dict = None):
        key = name + ("_" + "_".join(f"{k}={v}" for k, v in (labels or {}).items()) if labels else "")
        self._counters[key] += 1

    def observe(self, name: str, value: float, labels: dict = None):
        self._durations.append((name, value, labels or {}))

    def report(self):
        print("\n=== Retry Metrics ===")
        for key, count in sorted(self._counters.items()):
            print(f"  {key}: {count}")
        if self._durations:
            durations = [d[1] for d in self._durations]
            print(f"  call_duration_ms: p50={sorted(durations)[len(durations)//2]*1000:.0f} "
                  f"max={max(durations)*1000:.0f}")

metrics = RetryMetrics()

async def instrumented_retry(
    prompt: str,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
) -> str:
    metrics.inc("api_calls_total")
    call_start = time.monotonic()

    for attempt in range(max_attempts):
        attempt_start = time.monotonic()
        try:
            response = await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            metrics.inc("api_calls_success")
            metrics.observe("call_duration_s", time.monotonic() - call_start)
            if attempt > 0:
                metrics.inc("retry_success", {"attempt": str(attempt)})
            return response.content[0].text

        except RateLimitError as e:
            error_class = "rate_limit"
            metrics.inc("api_errors_total", {"class": error_class, "attempt": str(attempt)})

        except APIConnectionError as e:
            error_class = "connection"
            metrics.inc("api_errors_total", {"class": error_class, "attempt": str(attempt)})

        except APIStatusError as e:
            if e.status_code not in (429, 500, 502, 503, 529):
                metrics.inc("api_errors_total", {"class": f"http_{e.status_code}", "attempt": str(attempt)})
                raise
            error_class = f"http_{e.status_code}"
            metrics.inc("api_errors_total", {"class": error_class, "attempt": str(attempt)})

        if attempt < max_attempts - 1:
            cap = min(max_delay, base_delay * (2 ** attempt))
            delay = random.uniform(0, cap)
            metrics.inc("retries_total", {"attempt": str(attempt + 1)})
            print(f"[retry {attempt+1}/{max_attempts}: sleeping {delay:.2f}s]")
            await asyncio.sleep(delay)

    metrics.inc("api_calls_exhausted")
    raise RuntimeError(f"Exhausted {max_attempts} attempts")

async def run_batch_instrumented(prompts: list[str]) -> list[str]:
    tasks = [instrumented_retry(p) for p in prompts]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    metrics.report()
    return [r if not isinstance(r, Exception) else f"ERROR: {r}" for r in results]

if __name__ == "__main__":
    async def main():
        prompts = ["What is CAP theorem?", "Explain eventual consistency.", "What is Raft?"]
        results = await run_batch_instrumented(prompts)
        for p, r in zip(prompts, results):
            print(f"  {p[:40]}: {r[:80]}")
    asyncio.run(main())

# Expected Token Savings: Metrics identify which error classes dominate — enables targeted fixes vs. brute retry
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Jitter Strategy | Retry Budget | Observability | Best For |
|--------|----------------|--------------|---------------|----------|
| 1 | Full jitter | Per-call limit | Print only | Simple scripts, quick fixes |
| 2 | Full jitter + retry-after | Per-call limit | Print only | APIs that return Retry-After headers |
| 3 | Decorrelated jitter | Per-call limit | Print only | High-concurrency async workloads |
| 4 | Full jitter | Shared budget | Print only | Multi-agent systems with shared quota |
| 5 | Adaptive (success-rate driven) | Per-call limit | Print only | Long-running sessions needing self-tuning |
| 6 | Full jitter | Per-call limit | Prometheus metrics | Production systems needing retry observability |
