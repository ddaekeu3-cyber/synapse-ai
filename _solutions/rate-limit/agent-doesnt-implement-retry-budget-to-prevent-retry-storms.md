---
layout: solution
title: "Agent Doesn't Implement Retry Budget to Prevent Retry Storms"
category: rate-limit
description: "Unbounded retries amplify API failures into retry storms — every failing request spawns multiple retries, which compound rate limit errors and make recovery impossible. A retry budget caps total retry attempts per time window, protecting both the API and the calling service."
tags: [rate-limit, retry, backoff, resilience, overload, retry-storm]
---

## Problem

When Claude's API returns 429 or 529 errors, naive retry logic retries immediately and repeatedly. With many concurrent agents, each retrying 3-5 times, a transient rate limit event multiplies into 10-20x the original request volume — making recovery impossible and extending outages. A retry budget limits total retries per window, uses coordinated backoff, and fails fast when the budget is exhausted.

## Solutions

### Option 1: Simple Retry Budget with Leaky Bucket

```python
import anthropic
import time
import threading
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class RetryBudget:
    """
    Leaky bucket retry budget: N retries allowed per window.
    Prevents retry storms by capping total retries across all callers.
    """
    max_retries_per_window: int = 20
    window_seconds: float = 60.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _retry_timestamps: list[float] = field(default_factory=list)
    _total_retries_attempted: int = 0
    _total_retries_denied: int = 0

    def can_retry(self) -> bool:
        """Check if a retry is allowed within the current window."""
        now = time.time()
        with self._lock:
            # Prune old timestamps
            cutoff = now - self.window_seconds
            self._retry_timestamps = [t for t in self._retry_timestamps if t > cutoff]

            if len(self._retry_timestamps) < self.max_retries_per_window:
                self._retry_timestamps.append(now)
                self._total_retries_attempted += 1
                return True
            else:
                self._total_retries_denied += 1
                return False

    @property
    def retries_in_window(self) -> int:
        now = time.time()
        with self._lock:
            cutoff = now - self.window_seconds
            return sum(1 for t in self._retry_timestamps if t > cutoff)

    def stats(self) -> dict:
        return {
            "retries_in_window": self.retries_in_window,
            "max_per_window": self.max_retries_per_window,
            "total_attempted": self._total_retries_attempted,
            "total_denied": self._total_retries_denied,
            "budget_remaining": self.max_retries_per_window - self.retries_in_window
        }

# Global shared retry budget
_retry_budget = RetryBudget(max_retries_per_window=10, window_seconds=60.0)

def call_with_retry_budget(
    prompt: str,
    system: str = "",
    max_attempts: int = 3,
    base_delay: float = 1.0
) -> dict:
    """Call Claude API with retry budget enforcement."""
    last_error = None

    for attempt in range(max_attempts):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": prompt}]
            )
            return {"text": response.content[0].text, "attempts": attempt + 1, "success": True}

        except anthropic.RateLimitError as e:
            last_error = e
            if attempt < max_attempts - 1:
                if not _retry_budget.can_retry():
                    print(f"[RetryBudget] DENIED retry — budget exhausted "
                          f"({_retry_budget.retries_in_window}/{_retry_budget.max_retries_per_window})")
                    break  # Fail fast — don't storm the API

                delay = base_delay * (2 ** attempt)
                print(f"[RetryBudget] Attempt {attempt+1} failed (429), retry in {delay:.1f}s "
                      f"(budget: {_retry_budget.retries_in_window}/{_retry_budget.max_retries_per_window})")
                time.sleep(delay)
            continue

        except anthropic.APIStatusError as e:
            if e.status_code == 529:  # Overloaded
                last_error = e
                if attempt < max_attempts - 1 and _retry_budget.can_retry():
                    time.sleep(base_delay * (2 ** attempt))
                    continue
            raise

    return {
        "text": None,
        "attempts": max_attempts,
        "success": False,
        "error": str(last_error)[:80]
    }

# Usage
result = call_with_retry_budget("What is the capital of Japan?")
print(f"Result: {result['text'][:100] if result['text'] else 'FAILED'}")
print(f"Attempts: {result['attempts']}")
print(f"Budget stats: {_retry_budget.stats()}")

# Expected Token Savings: Failing fast instead of storming saves N-1 retry tokens per request
# Environment: ANTHROPIC_API_KEY required
```

### Option 2: Distributed Retry Budget with SQLite Coordination

```python
import anthropic
import sqlite3
import time
import os
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

DB_PATH = "/tmp/retry_budget.db"

def init_retry_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS retry_budget (
                window_key TEXT PRIMARY KEY,
                retry_count INTEGER DEFAULT 0,
                window_start REAL NOT NULL,
                window_end REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS retry_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_id TEXT,
                timestamp REAL,
                allowed INTEGER,
                window_count INTEGER
            )
        """)

init_retry_db()

@dataclass
class DistributedRetryBudget:
    max_retries: int = 50      # Across all instances
    window_seconds: float = 60.0
    instance_id: str = ""

    def __post_init__(self):
        self.instance_id = self.instance_id or f"inst_{os.getpid()}"

    def _window_key(self) -> str:
        window_start = int(time.time() / self.window_seconds) * self.window_seconds
        return f"window_{window_start:.0f}"

    def request_retry(self) -> tuple[bool, int]:
        """Atomically check and consume retry budget. Returns (allowed, current_count)."""
        now = time.time()
        window_key = self._window_key()
        window_start = int(now / self.window_seconds) * self.window_seconds
        window_end = window_start + self.window_seconds

        with sqlite3.connect(DB_PATH) as conn:
            # Upsert window record
            conn.execute("""
                INSERT INTO retry_budget (window_key, retry_count, window_start, window_end)
                VALUES (?, 0, ?, ?)
                ON CONFLICT(window_key) DO NOTHING
            """, (window_key, window_start, window_end))

            # Atomic increment if under budget
            row = conn.execute(
                "SELECT retry_count FROM retry_budget WHERE window_key=?",
                (window_key,)
            ).fetchone()
            current = row[0] if row else 0

            if current < self.max_retries:
                conn.execute(
                    "UPDATE retry_budget SET retry_count=retry_count+1 WHERE window_key=?",
                    (window_key,)
                )
                allowed = True
                new_count = current + 1
            else:
                allowed = False
                new_count = current

            conn.execute(
                "INSERT INTO retry_log (caller_id, timestamp, allowed, window_count) VALUES (?,?,?,?)",
                (self.instance_id, now, int(allowed), new_count)
            )

        return allowed, new_count

    def get_stats(self) -> dict:
        window_key = self._window_key()
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute(
                "SELECT retry_count FROM retry_budget WHERE window_key=?", (window_key,)
            ).fetchone()
            count = row[0] if row else 0

            denied = conn.execute(
                "SELECT COUNT(*) FROM retry_log WHERE allowed=0 AND timestamp > ?",
                (time.time() - self.window_seconds,)
            ).fetchone()[0]

        return {
            "window_retries": count,
            "max_retries": self.max_retries,
            "budget_remaining": self.max_retries - count,
            "denied_in_window": denied,
            "instance_id": self.instance_id
        }

budget = DistributedRetryBudget(max_retries=5, window_seconds=60.0)

def resilient_call(prompt: str, max_attempts: int = 3) -> dict:
    """API call with distributed retry budget enforcement."""
    for attempt in range(max_attempts):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}]
            )
            return {"text": response.content[0].text, "attempts": attempt + 1}

        except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
            is_retryable = isinstance(e, anthropic.RateLimitError) or getattr(e, 'status_code', 0) == 529
            if not is_retryable or attempt == max_attempts - 1:
                raise

            allowed, count = budget.request_retry()
            if not allowed:
                print(f"[DistributedBudget] Retry DENIED — global budget at {count}/{budget.max_retries}")
                raise RuntimeError(f"Retry budget exhausted: {count}/{budget.max_retries} retries in window")

            delay = 2 ** attempt
            print(f"[DistributedBudget] Retry {count}/{budget.max_retries} — sleeping {delay}s")
            time.sleep(delay)

result = resilient_call("Explain recursion in one sentence.")
print(f"Result: {result['text'][:100]}")
print(f"Budget: {budget.get_stats()}")

# Expected Token Savings: Distributed budget prevents N-instance retry storms
# Environment: ANTHROPIC_API_KEY required, writes to /tmp/retry_budget.db
```

### Option 3: Async Retry Budget with Circuit Breaker Integration

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.AsyncAnthropic()

class BudgetState(str, Enum):
    HEALTHY = "healthy"        # Retries allowed
    STRESSED = "stressed"      # Retries allowed but reduced
    EXHAUSTED = "exhausted"    # No more retries
    RECOVERING = "recovering"  # Partial budget restored

@dataclass
class AsyncRetryBudget:
    max_per_minute: int = 30
    stress_threshold: float = 0.7   # At 70% usage → stressed state
    recovery_seconds: float = 30.0  # Time before recovery starts

    _retry_times: list[float] = field(default_factory=list)
    _exhausted_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def state(self) -> BudgetState:
        async with self._lock:
            now = time.time()
            cutoff = now - 60.0
            self._retry_times = [t for t in self._retry_times if t > cutoff]
            used = len(self._retry_times)
            usage_rate = used / self.max_per_minute

            if self._exhausted_at > 0:
                if now - self._exhausted_at > self.recovery_seconds:
                    self._exhausted_at = 0  # Reset
                    return BudgetState.RECOVERING
                return BudgetState.EXHAUSTED

            if usage_rate >= 1.0:
                self._exhausted_at = now
                return BudgetState.EXHAUSTED
            elif usage_rate >= self.stress_threshold:
                return BudgetState.STRESSED
            return BudgetState.HEALTHY

    async def consume(self) -> tuple[bool, BudgetState]:
        """Try to consume one retry unit. Returns (allowed, state)."""
        current_state = await self.state()

        if current_state == BudgetState.EXHAUSTED:
            return False, current_state

        async with self._lock:
            self._retry_times.append(time.time())
            return True, current_state

    def _get_delay(self, attempt: int, state: BudgetState) -> float:
        base = 2 ** attempt
        multipliers = {
            BudgetState.HEALTHY: 1.0,
            BudgetState.STRESSED: 2.0,    # Double delay when stressed
            BudgetState.RECOVERING: 3.0,  # Triple delay during recovery
            BudgetState.EXHAUSTED: 0.0    # Won't retry anyway
        }
        return base * multipliers.get(state, 1.0)

    async def stats(self) -> dict:
        async with self._lock:
            now = time.time()
            cutoff = now - 60.0
            self._retry_times = [t for t in self._retry_times if t > cutoff]
            used = len(self._retry_times)
        return {
            "used_per_minute": used,
            "max_per_minute": self.max_per_minute,
            "usage_pct": round(used / self.max_per_minute * 100),
            "state": (await self.state()).value
        }

budget = AsyncRetryBudget(max_per_minute=20, stress_threshold=0.6)

async def async_call_with_budget(prompt: str, max_attempts: int = 3) -> dict:
    for attempt in range(max_attempts):
        try:
            response = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=100,
                messages=[{"role": "user", "content": prompt}]
            )
            return {"text": response.content[0].text, "attempts": attempt + 1, "success": True}

        except anthropic.RateLimitError:
            if attempt == max_attempts - 1:
                break

            allowed, state = await budget.consume()
            if not allowed:
                print(f"[AsyncBudget] Retry budget EXHAUSTED — failing fast")
                break

            delay = budget._get_delay(attempt, state)
            print(f"[AsyncBudget] Retry {attempt+1} (state: {state.value}, delay: {delay:.1f}s)")
            await asyncio.sleep(delay)

    return {"text": None, "attempts": max_attempts, "success": False}

async def main():
    # Simulate concurrent requests
    prompts = [f"What is {i}+{i}?" for i in range(5)]
    results = await asyncio.gather(*[async_call_with_budget(p) for p in prompts])

    for i, r in enumerate(results):
        status = "✓" if r["success"] else "✗"
        print(f"[{status}] prompt_{i}: attempts={r['attempts']}, text={str(r['text'])[:40]}")

    print(f"\nBudget: {await budget.stats()}")

asyncio.run(main())

# Expected Token Savings: State-aware delay prevents thundering herd; stressed state doubles backoff
# Environment: ANTHROPIC_API_KEY required, uses asyncio
```

### Option 4: Per-Caller Retry Quota with Priority Classes

```python
import anthropic
import time
import threading
from dataclasses import dataclass, field
from enum import Enum

client = anthropic.Anthropic()

class CallerPriority(str, Enum):
    CRITICAL = "critical"   # e.g., payment flows — higher quota
    STANDARD = "standard"   # Normal requests
    BATCH = "batch"         # Background jobs — lowest quota

PRIORITY_QUOTAS = {
    CallerPriority.CRITICAL: 20,   # 20 retries/minute
    CallerPriority.STANDARD: 10,   # 10 retries/minute
    CallerPriority.BATCH: 3,       # 3 retries/minute — prevent batch jobs from starving others
}

@dataclass
class PerCallerRetryQuota:
    _quotas: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    window_seconds: float = 60.0

    def _get_caller_limit(self, caller_id: str, priority: CallerPriority) -> int:
        return PRIORITY_QUOTAS[priority]

    def request_retry(self, caller_id: str, priority: CallerPriority = CallerPriority.STANDARD) -> tuple[bool, dict]:
        now = time.time()
        limit = self._get_caller_limit(caller_id, priority)
        cutoff = now - self.window_seconds

        with self._lock:
            if caller_id not in self._quotas:
                self._quotas[caller_id] = []

            # Prune old timestamps for this caller
            self._quotas[caller_id] = [t for t in self._quotas[caller_id] if t > cutoff]
            used = len(self._quotas[caller_id])

            if used < limit:
                self._quotas[caller_id].append(now)
                return True, {"caller": caller_id, "used": used + 1, "limit": limit, "priority": priority.value}
            else:
                return False, {"caller": caller_id, "used": used, "limit": limit, "priority": priority.value}

    def get_all_stats(self) -> dict:
        now = time.time()
        cutoff = now - self.window_seconds
        with self._lock:
            return {
                caller: {
                    "retries_used": len([t for t in times if t > cutoff]),
                    "quota": PRIORITY_QUOTAS.get(CallerPriority.STANDARD, 10)
                }
                for caller, times in self._quotas.items()
            }

quota_manager = PerCallerRetryQuota()

def prioritized_call(
    caller_id: str,
    prompt: str,
    priority: CallerPriority = CallerPriority.STANDARD,
    max_attempts: int = 3
) -> dict:
    """Make API call with per-caller priority-based retry quota."""
    for attempt in range(max_attempts):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": prompt}]
            )
            return {
                "text": response.content[0].text,
                "caller": caller_id,
                "priority": priority.value,
                "attempts": attempt + 1,
                "success": True
            }

        except anthropic.RateLimitError:
            if attempt == max_attempts - 1:
                break

            allowed, info = quota_manager.request_retry(caller_id, priority)
            if not allowed:
                print(f"[QuotaManager] {caller_id} ({priority.value}) DENIED "
                      f"— quota exhausted {info['used']}/{info['limit']}")
                break

            delay = 2 ** attempt
            print(f"[QuotaManager] {caller_id} retry {info['used']}/{info['limit']} — sleeping {delay}s")
            time.sleep(delay)

    return {"text": None, "caller": caller_id, "priority": priority.value,
            "attempts": max_attempts, "success": False}

# Simulate callers with different priorities
callers = [
    ("payment_service", CallerPriority.CRITICAL, "Confirm payment of $99 for order 123"),
    ("chat_bot", CallerPriority.STANDARD, "What is 2+2?"),
    ("batch_job", CallerPriority.BATCH, "Summarize document 1"),
    ("chat_bot_2", CallerPriority.STANDARD, "Tell me a joke"),
    ("batch_job_2", CallerPriority.BATCH, "Summarize document 2"),
]

for caller_id, priority, prompt in callers:
    result = prioritized_call(caller_id, prompt, priority)
    status = "✓" if result["success"] else "✗"
    print(f"[{status}] [{priority.value}] {caller_id}: {str(result['text'])[:50]}")

print(f"\nAll quotas: {quota_manager.get_all_stats()}")

# Expected Token Savings: Batch callers starved first; critical flows retain retry capacity
# Environment: ANTHROPIC_API_KEY required
```

### Option 5: Retry Storm Detector with Auto-Backpressure

```python
import anthropic
import time
import threading
from dataclasses import dataclass, field
from collections import deque

client = anthropic.Anthropic()

@dataclass
class RetryStormDetector:
    """
    Detects retry storms and applies progressive backpressure.
    A storm is detected when retry rate exceeds N times the normal rate.
    """
    baseline_rps: float = 5.0          # Expected normal retry rate
    storm_multiplier: float = 3.0       # Storm if retry_rate > baseline * multiplier
    observation_window: float = 30.0    # Window to measure retry rate
    backpressure_max_delay: float = 30.0

    _retry_events: deque = field(default_factory=lambda: deque())
    _storm_active: bool = False
    _storm_started_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def current_retry_rate(self) -> float:
        """Retries per second in observation window."""
        now = time.time()
        cutoff = now - self.observation_window
        with self._lock:
            recent = sum(1 for t in self._retry_events if t > cutoff)
        return recent / self.observation_window

    def record_retry(self) -> float:
        """Record a retry attempt. Returns recommended delay (0 = no storm)."""
        now = time.time()
        with self._lock:
            self._retry_events.append(now)
            # Prune old events
            cutoff = now - self.observation_window * 2
            while self._retry_events and self._retry_events[0] < cutoff:
                self._retry_events.popleft()

        retry_rate = self.current_retry_rate
        storm_threshold = self.baseline_rps * self.storm_multiplier

        if retry_rate >= storm_threshold:
            if not self._storm_active:
                self._storm_active = True
                self._storm_started_at = now
                print(f"[StormDetector] STORM DETECTED! Rate: {retry_rate:.1f}/s "
                      f"(threshold: {storm_threshold:.1f}/s)")

            # Progressive backpressure: longer delay the longer storm continues
            storm_duration = now - self._storm_started_at
            delay = min(
                self.backpressure_max_delay,
                1.0 + storm_duration / 10.0  # 1s + 0.1s per second of storm
            )
            return delay
        else:
            if self._storm_active:
                print(f"[StormDetector] Storm subsided (rate: {retry_rate:.1f}/s)")
                self._storm_active = False
            return 0.0  # No backpressure

    def status(self) -> dict:
        return {
            "retry_rate_per_sec": round(self.current_retry_rate, 2),
            "storm_threshold": self.baseline_rps * self.storm_multiplier,
            "storm_active": self._storm_active,
            "storm_duration_s": round(time.time() - self._storm_started_at) if self._storm_active else 0
        }

detector = RetryStormDetector(baseline_rps=2.0, storm_multiplier=2.0)

def storm_safe_call(prompt: str, max_attempts: int = 3) -> dict:
    """API call that detects and responds to retry storms."""
    for attempt in range(max_attempts):
        try:
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=80,
                messages=[{"role": "user", "content": prompt}]
            )
            return {"text": response.content[0].text, "attempts": attempt + 1}

        except anthropic.RateLimitError:
            if attempt == max_attempts - 1:
                break

            backpressure_delay = detector.record_retry()
            base_delay = 2 ** attempt

            if backpressure_delay > 0:
                total_delay = backpressure_delay + base_delay
                print(f"[StormSafe] Backpressure delay: {total_delay:.1f}s (storm active)")
                time.sleep(total_delay)
            else:
                time.sleep(base_delay)

    return {"text": None, "attempts": max_attempts, "success": False}

# Simulate normal operation
print("Normal calls:")
for i in range(3):
    result = storm_safe_call(f"What is {i}+{i}?")
    print(f"  Call {i}: {str(result.get('text','FAILED'))[:40]}")

print(f"\nDetector status: {detector.status()}")

# Expected Token Savings: Storm detection adds adaptive delay; prevents exponential retry amplification
# Environment: ANTHROPIC_API_KEY required
```

### Option 6: Jitter + Budget + Retry-After Header Respect

```python
import anthropic
import time
import random
from dataclasses import dataclass, field

client = anthropic.Anthropic()

@dataclass
class SmartRetryPolicy:
    """
    Combines: retry budget + exponential backoff + jitter + Retry-After header respect.
    """
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter_factor: float = 0.3      # Add ±30% random jitter
    budget_window: float = 60.0
    budget_max: int = 15

    _budget_timestamps: list[float] = field(default_factory=list)
    _total_calls: int = 0
    _total_retries: int = 0
    _retry_after_violations: int = 0

    def _budget_remaining(self) -> int:
        now = time.time()
        cutoff = now - self.budget_window
        self._budget_timestamps = [t for t in self._budget_timestamps if t > cutoff]
        return self.budget_max - len(self._budget_timestamps)

    def _consume_budget(self) -> bool:
        if self._budget_remaining() > 0:
            self._budget_timestamps.append(time.time())
            return True
        return False

    def _get_delay(self, attempt: int, retry_after: float = 0.0) -> float:
        """Compute delay: max(retry_after, exponential_backoff) + jitter."""
        exp_delay = min(self.base_delay * (2 ** attempt), self.max_delay)
        jitter = exp_delay * self.jitter_factor * (2 * random.random() - 1)
        computed = exp_delay + jitter
        # Respect Retry-After if it's longer
        return max(computed, retry_after)

    def _parse_retry_after(self, error: anthropic.APIStatusError) -> float:
        """Parse Retry-After header from rate limit response."""
        try:
            headers = getattr(error, 'response', None)
            if headers:
                retry_after_str = headers.headers.get('retry-after') or \
                                   response.headers.get('x-ratelimit-reset-requests')
                if retry_after_str:
                    return float(retry_after_str)
        except Exception:
            pass
        return 0.0

    def execute(self, fn, *args, **kwargs) -> dict:
        """Execute function with smart retry policy."""
        self._total_calls += 1
        last_error = None

        for attempt in range(self.max_retries + 1):
            try:
                result = fn(*args, **kwargs)
                return {"result": result, "attempts": attempt + 1, "success": True,
                        "retries_used": attempt}

            except (anthropic.RateLimitError, anthropic.APIStatusError) as e:
                is_retryable = isinstance(e, anthropic.RateLimitError) or \
                               (hasattr(e, 'status_code') and e.status_code in (429, 529))

                if not is_retryable or attempt == self.max_retries:
                    last_error = e
                    break

                # Check retry budget
                if not self._consume_budget():
                    print(f"[SmartRetry] Budget exhausted — failing fast "
                          f"(0/{self.budget_max} remaining)")
                    last_error = e
                    break

                # Parse Retry-After header
                retry_after = self._parse_retry_after(e) if isinstance(e, anthropic.APIStatusError) else 0.0
                delay = self._get_delay(attempt, retry_after)

                self._total_retries += 1
                budget_left = self._budget_remaining()

                print(f"[SmartRetry] Attempt {attempt+1} failed, "
                      f"delay={delay:.2f}s (jittered), budget={budget_left} left")
                time.sleep(delay)

            except Exception as e:
                last_error = e
                break

        return {
            "result": None,
            "attempts": self.max_retries + 1,
            "success": False,
            "error": str(last_error)[:80]
        }

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "total_retries": self._total_retries,
            "retry_rate": round(self._total_retries / max(self._total_calls, 1), 3),
            "budget_remaining": self._budget_remaining(),
            "budget_max": self.budget_max
        }

policy = SmartRetryPolicy(max_retries=3, base_delay=0.5, budget_max=10)

def make_api_call(prompt: str):
    """Thin wrapper for policy.execute."""
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=80,
        messages=[{"role": "user", "content": prompt}]
    )
    return response.content[0].text

# Usage
for prompt in ["What is 7*6?", "Name a planet", "Explain DNS in one line"]:
    result = policy.execute(make_api_call, prompt)
    if result["success"]:
        print(f"✓ {result['result'][:60]} (attempts: {result['attempts']})")
    else:
        print(f"✗ Failed: {result['error']}")

print(f"\nPolicy stats: {policy.stats()}")

# Expected Token Savings: Jitter prevents synchronized retry bursts; budget caps total exposure
# Environment: ANTHROPIC_API_KEY required
```

## Comparison

| Option | Budget Scope | Cross-Process | Priority Support | Storm Detection | Best Use Case |
|--------|-------------|---------------|-----------------|-----------------|---------------|
| Leaky Bucket Budget | Single process | No | No | No | Single-instance agents |
| SQLite Distributed | Multi-process | Yes | No | No | Multi-worker deployments |
| Async with Circuit Breaker | Single async loop | No | No | Via state | Async high-concurrency agents |
| Per-Caller Priority Quota | Per caller | No | Yes | No | Multi-tenant APIs |
| Storm Detector | Single process | No | No | Yes | Services with burst traffic |
| Jitter + Budget + Retry-After | Single process | No | No | No | Production default (most complete) |
