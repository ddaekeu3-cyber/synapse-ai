---
title: "Agent Doesn't Implement Predictive Rate Limit Budgeting"
description: "How to predict future API usage based on current request patterns and proactively budget rate limit tokens to avoid 429 errors before they happen."
categories: [rate-limit]
difficulty: intermediate
---

Reactive rate limiting waits for a 429 before backing off. Predictive budgeting tracks consumption patterns, forecasts when the limit will be reached, and proactively throttles or queues requests before the limit is hit—keeping latency smooth and eliminating the retry tax.

## Solution 1: Rolling Window Token Consumption Tracker

Track token consumption in a rolling window and compute the projected exhaustion time.

```python
import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

# Anthropic rate limits (adjust to your actual tier)
TOKENS_PER_MINUTE_LIMIT = 100_000
REQUESTS_PER_MINUTE_LIMIT = 60
WINDOW_SECONDS = 60.0
SAFETY_MARGIN = 0.85  # Start throttling at 85% of limit


@dataclass
class TokenUsageRecord:
    timestamp: float
    tokens: int
    request_id: str


class RollingWindowTracker:
    def __init__(self, window_seconds: float = WINDOW_SECONDS):
        self._window = window_seconds
        self._records: deque[TokenUsageRecord] = deque()
        self._lock = asyncio.Lock()

    async def record(self, tokens: int, request_id: str = ""):
        async with self._lock:
            now = time.monotonic()
            self._records.append(TokenUsageRecord(now, tokens, request_id))
            self._prune(now)

    def _prune(self, now: float):
        cutoff = now - self._window
        while self._records and self._records[0].timestamp < cutoff:
            self._records.popleft()

    async def current_usage(self) -> tuple[int, int]:
        """Returns (tokens_used_in_window, request_count_in_window)."""
        async with self._lock:
            self._prune(time.monotonic())
            tokens = sum(r.tokens for r in self._records)
            return tokens, len(self._records)

    async def projected_exhaustion_seconds(self) -> float | None:
        """Returns seconds until token limit is hit at current rate, or None if safe."""
        tokens_used, _ = await self.current_usage()
        if tokens_used == 0:
            return None
        tokens_remaining = TOKENS_PER_MINUTE_LIMIT - tokens_used
        if tokens_remaining <= 0:
            return 0.0
        # Rate: tokens per second in current window
        rate = tokens_used / self._window
        if rate <= 0:
            return None
        time_to_exhaust = tokens_remaining / rate
        return time_to_exhaust

    async def should_throttle(self) -> tuple[bool, float]:
        """Returns (should_throttle, delay_seconds)."""
        tokens_used, req_count = await self.current_usage()
        token_ratio = tokens_used / TOKENS_PER_MINUTE_LIMIT
        req_ratio = req_count / REQUESTS_PER_MINUTE_LIMIT

        if max(token_ratio, req_ratio) >= SAFETY_MARGIN:
            exhaustion = await self.projected_exhaustion_seconds()
            delay = max(0.5, (exhaustion or 1.0) * 0.1)
            return True, delay

        return False, 0.0


tracker = RollingWindowTracker()


async def rate_limited_call(prompt: str) -> str:
    throttle, delay = await tracker.should_throttle()
    if throttle:
        print(f"[rate] Throttling for {delay:.1f}s (predictive budget)")
        await asyncio.sleep(delay)

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    tokens_used = resp.usage.input_tokens + resp.usage.output_tokens
    await tracker.record(tokens_used)

    usage, reqs = await tracker.current_usage()
    print(f"[rate] Window usage: {usage}/{TOKENS_PER_MINUTE_LIMIT} tokens, {reqs}/{REQUESTS_PER_MINUTE_LIMIT} reqs")
    return resp.content[0].text


async def main():
    prompts = [f"What is {i}+{i}?" for i in range(10)]
    results = await asyncio.gather(*[rate_limited_call(p) for p in prompts])
    print(f"Completed {len(results)} calls")


asyncio.run(main())
```

## Solution 2: Token Budget Pre-Estimator with Request Queuing

Estimate token cost before sending, check the budget, and queue requests that would exceed it.

```python
import asyncio
import time
from dataclasses import dataclass, field
from collections import deque
import anthropic

client = anthropic.AsyncAnthropic()

MINUTE_TOKEN_BUDGET = 50_000
CHARS_PER_TOKEN_APPROX = 4


@dataclass
class PendingRequest:
    prompt: str
    estimated_tokens: int
    result_future: asyncio.Future = field(default_factory=asyncio.get_event_loop)

    def __post_init__(self):
        self.result_future = asyncio.get_event_loop().create_future()


class BudgetAwareQueue:
    def __init__(self, budget: int = MINUTE_TOKEN_BUDGET):
        self._budget = budget
        self._used = 0
        self._reset_at = time.monotonic() + 60.0
        self._queue: deque[PendingRequest] = deque()
        self._lock = asyncio.Lock()

    def estimate_tokens(self, prompt: str, max_tokens: int = 256) -> int:
        input_est = len(prompt) // CHARS_PER_TOKEN_APPROX
        return input_est + max_tokens

    def _maybe_reset(self):
        now = time.monotonic()
        if now >= self._reset_at:
            self._used = 0
            self._reset_at = now + 60.0
            print(f"[budget] Window reset at {time.strftime('%H:%M:%S')}")

    async def acquire(self, estimated_tokens: int) -> float:
        """Wait until the budget can accommodate estimated_tokens. Returns wait time."""
        wait_start = time.monotonic()
        while True:
            async with self._lock:
                self._maybe_reset()
                if self._used + estimated_tokens <= self._budget:
                    self._used += estimated_tokens
                    return time.monotonic() - wait_start
            # Budget exhausted — wait for reset
            sleep_for = max(0.1, self._reset_at - time.monotonic())
            print(f"[budget] Budget full ({self._used}/{self._budget}). Waiting {sleep_for:.1f}s")
            await asyncio.sleep(sleep_for)

    async def release_overage(self, actual_tokens: int, estimated_tokens: int):
        """Return unused estimated tokens to the budget."""
        async with self._lock:
            overage = estimated_tokens - actual_tokens
            if overage > 0:
                self._used = max(0, self._used - overage)


budget_queue = BudgetAwareQueue()


async def budgeted_call(prompt: str, max_tokens: int = 256) -> str:
    estimated = budget_queue.estimate_tokens(prompt, max_tokens)
    wait = await budget_queue.acquire(estimated)
    if wait > 0.1:
        print(f"[budget] Waited {wait:.1f}s for budget slot")

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    actual = resp.usage.input_tokens + resp.usage.output_tokens
    await budget_queue.release_overage(actual, estimated)

    return resp.content[0].text


async def main():
    prompts = [f"Tell me one fact about the number {i}." for i in range(8)]
    results = await asyncio.gather(*[budgeted_call(p) for p in prompts])
    print(f"All {len(results)} calls completed")


asyncio.run(main())
```

## Solution 3: Exponential Moving Average Rate Predictor

Use an exponential moving average (EMA) to smooth token consumption rate and predict future usage.

```python
import asyncio
import time
import math
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

LIMIT_TOKENS_PER_MINUTE = 100_000
EMA_ALPHA = 0.3   # Smoothing factor (higher = faster adaptation)


@dataclass
class EMAPredictor:
    ema_tokens_per_second: float = 0.0
    last_update: float = field(default_factory=time.monotonic)
    samples: int = 0

    def update(self, tokens: int, elapsed_seconds: float):
        if elapsed_seconds <= 0:
            return
        instantaneous_rate = tokens / elapsed_seconds
        if self.samples == 0:
            self.ema_tokens_per_second = instantaneous_rate
        else:
            self.ema_tokens_per_second = (
                EMA_ALPHA * instantaneous_rate
                + (1 - EMA_ALPHA) * self.ema_tokens_per_second
            )
        self.samples += 1
        self.last_update = time.monotonic()

    def seconds_until_limit(self, current_used: int) -> float | None:
        """Predict how many seconds until the per-minute limit is hit."""
        if self.ema_tokens_per_second <= 0:
            return None
        tokens_remaining = LIMIT_TOKENS_PER_MINUTE - current_used
        if tokens_remaining <= 0:
            return 0.0
        return tokens_remaining / self.ema_tokens_per_second

    def recommended_delay(self, current_used: int, desired_safety_buffer: float = 15.0) -> float:
        """Return a recommended delay in seconds to stay within budget."""
        exhaustion = self.seconds_until_limit(current_used)
        if exhaustion is None:
            return 0.0
        if exhaustion < desired_safety_buffer:
            # We're approaching the limit — slow down proportionally
            return max(0, (desired_safety_buffer - exhaustion) * 0.1)
        return 0.0


predictor = EMAPredictor()
window_used = 0
window_start = time.monotonic()


async def predicted_rate_call(prompt: str) -> str:
    global window_used, window_start

    # Reset window after 60s
    now = time.monotonic()
    if now - window_start >= 60.0:
        window_used = 0
        window_start = now

    delay = predictor.recommended_delay(window_used)
    if delay > 0:
        print(f"[ema] Predictive delay: {delay:.2f}s (rate={predictor.ema_tokens_per_second:.0f} tok/s)")
        await asyncio.sleep(delay)

    t0 = time.monotonic()
    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed = time.monotonic() - t0
    tokens = resp.usage.input_tokens + resp.usage.output_tokens

    predictor.update(tokens, elapsed)
    window_used += tokens

    return resp.content[0].text


async def main():
    prompts = [f"Briefly define term #{i} in computer science." for i in range(12)]
    results = await asyncio.gather(*[predicted_rate_call(p) for p in prompts])
    print(f"Completed {len(results)} calls. Final EMA rate: {predictor.ema_tokens_per_second:.1f} tok/s")


asyncio.run(main())
```

## Solution 4: Cost-Aware Request Prioritization

Assign each request a priority and a token budget; high-priority requests get budget first.

```python
import asyncio
import heapq
import time
from dataclasses import dataclass, field
from typing import Any
import anthropic

client = anthropic.AsyncAnthropic()

MINUTE_TOKEN_BUDGET = 50_000


@dataclass(order=True)
class PrioritizedRequest:
    priority: int           # Lower = higher priority (min-heap)
    enqueued_at: float = field(compare=False, default_factory=time.monotonic)
    prompt: str = field(compare=False, default="")
    max_tokens: int = field(compare=False, default=256)
    future: Any = field(compare=False, default=None)

    def __post_init__(self):
        if self.future is None:
            self.future = asyncio.get_event_loop().create_future()


class PriorityBudgetScheduler:
    def __init__(self, budget: int = MINUTE_TOKEN_BUDGET):
        self._budget = budget
        self._used = 0
        self._reset_at = time.monotonic() + 60.0
        self._heap: list[PrioritizedRequest] = []
        self._lock = asyncio.Lock()
        self._not_empty = asyncio.Event()

    def _reset_if_needed(self):
        if time.monotonic() >= self._reset_at:
            self._used = 0
            self._reset_at = time.monotonic() + 60.0

    async def submit(self, prompt: str, priority: int = 5, max_tokens: int = 256) -> str:
        req = PrioritizedRequest(priority=priority, prompt=prompt, max_tokens=max_tokens)
        async with self._lock:
            heapq.heappush(self._heap, req)
            self._not_empty.set()
        return await req.future

    async def run_scheduler(self):
        """Background scheduler that processes requests in priority order."""
        while True:
            await self._not_empty.wait()
            async with self._lock:
                self._reset_if_needed()
                if not self._heap:
                    self._not_empty.clear()
                    continue
                # Peek at top-priority request
                top = self._heap[0]
                estimated = len(top.prompt) // 4 + top.max_tokens
                if self._used + estimated > self._budget:
                    # Budget exhausted — wait for reset
                    wait = max(0.5, self._reset_at - time.monotonic())
                    await asyncio.sleep(wait)
                    continue
                req = heapq.heappop(self._heap)
                self._used += estimated
                if not self._heap:
                    self._not_empty.clear()

            # Execute outside lock
            try:
                resp = await client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=req.max_tokens,
                    messages=[{"role": "user", "content": req.prompt}],
                )
                req.future.set_result(resp.content[0].text)
            except Exception as e:
                req.future.set_exception(e)


scheduler = PriorityBudgetScheduler()


async def main():
    asyncio.create_task(scheduler.run_scheduler())

    # Submit requests with different priorities
    tasks = [
        scheduler.submit("Critical: system status?", priority=1),       # Highest priority
        scheduler.submit("What is Python?", priority=5),                  # Normal
        scheduler.submit("Tell me a fun fact.", priority=9),              # Low priority
        scheduler.submit("Emergency: API health check", priority=1),     # Highest priority
    ]

    results = await asyncio.gather(*tasks)
    for i, r in enumerate(results):
        print(f"Result {i+1}: {r[:80]}")


asyncio.run(main())
```

## Solution 5: Adaptive Budget Scaler Based on Time-of-Day

Scale the effective rate limit budget based on historical usage patterns (lower during peak hours).

```python
import asyncio
import time
import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()

HARD_LIMIT = 100_000   # True API limit (tokens per minute)
USAGE_HISTORY_PATH = Path("/tmp/rate_limit_history.json")


@dataclass
class HourlyStats:
    hour: int
    avg_tokens_per_minute: float = 0.0
    peak_tokens_per_minute: float = 0.0
    samples: int = 0


def load_history() -> dict[int, HourlyStats]:
    if not USAGE_HISTORY_PATH.exists():
        return {}
    try:
        data = json.loads(USAGE_HISTORY_PATH.read_text())
        return {int(h): HourlyStats(**s) for h, s in data.items()}
    except Exception:
        return {}


def save_history(history: dict[int, HourlyStats]):
    USAGE_HISTORY_PATH.write_text(
        json.dumps({h: s.__dict__ for h, s in history.items()}, indent=2)
    )


def get_effective_budget(history: dict[int, HourlyStats]) -> int:
    """Compute an effective budget for the current hour based on history."""
    hour = time.localtime().tm_hour
    stats = history.get(hour)
    if not stats or stats.samples < 3:
        # Not enough history — use conservative 70% of hard limit
        return int(HARD_LIMIT * 0.70)
    # Reserve headroom based on peak historical usage
    headroom = max(5_000, int(stats.peak_tokens_per_minute * 1.2))
    return max(10_000, HARD_LIMIT - headroom)


def update_history(history: dict[int, HourlyStats], tokens_per_minute: float):
    hour = time.localtime().tm_hour
    if hour not in history:
        history[hour] = HourlyStats(hour=hour)
    s = history[hour]
    # Exponential moving average
    alpha = 0.3
    s.avg_tokens_per_minute = alpha * tokens_per_minute + (1 - alpha) * s.avg_tokens_per_minute
    s.peak_tokens_per_minute = max(s.peak_tokens_per_minute * 0.95, tokens_per_minute)
    s.samples += 1


history = load_history()
_window_tokens = 0
_window_start = time.monotonic()


async def adaptive_rate_call(prompt: str) -> str:
    global _window_tokens, _window_start

    now = time.monotonic()
    elapsed = now - _window_start
    if elapsed >= 60.0:
        tpm = _window_tokens / max(elapsed / 60, 1)
        update_history(history, tpm)
        save_history(history)
        _window_tokens = 0
        _window_start = now

    budget = get_effective_budget(history)
    if _window_tokens >= budget:
        wait = max(1.0, 60.0 - (time.monotonic() - _window_start))
        print(f"[adaptive] Budget {_window_tokens}/{budget} — waiting {wait:.1f}s")
        await asyncio.sleep(wait)

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    tokens = resp.usage.input_tokens + resp.usage.output_tokens
    _window_tokens += tokens
    return resp.content[0].text


async def main():
    budget = get_effective_budget(history)
    print(f"[adaptive] Effective budget for this hour: {budget}/{HARD_LIMIT} tokens/min")
    results = await asyncio.gather(*[adaptive_rate_call(f"Define: term_{i}") for i in range(5)])
    print(f"Completed {len(results)} calls. Window usage: {_window_tokens}")


asyncio.run(main())
```

## Solution 6: Headroom Alert with Auto-Throttle

Monitor remaining budget and emit structured alerts when headroom drops below thresholds.

```python
import asyncio
import json
import time
from dataclasses import dataclass, field
import anthropic

client = anthropic.AsyncAnthropic()

LIMIT = 100_000
ALERT_THRESHOLDS = [
    (0.90, "WARNING", "Rate limit 90% exhausted — throttling new requests"),
    (0.95, "ERROR",   "Rate limit 95% exhausted — queuing non-urgent requests"),
    (1.00, "CRITICAL","Rate limit exhausted — all requests blocked"),
]


@dataclass
class BudgetAlert:
    level: str
    message: str
    tokens_used: int
    tokens_limit: int
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps({
            "level": self.level,
            "message": self.message,
            "tokens_used": self.tokens_used,
            "tokens_limit": self.tokens_limit,
            "usage_pct": round(self.tokens_used / self.tokens_limit * 100, 1),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.timestamp)),
        })


class HeadroomMonitor:
    def __init__(self, limit: int = LIMIT):
        self._limit = limit
        self._used = 0
        self._reset_at = time.monotonic() + 60.0
        self._fired_alerts: set[str] = set()

    def reset_if_needed(self):
        if time.monotonic() >= self._reset_at:
            self._used = 0
            self._reset_at = time.monotonic() + 60.0
            self._fired_alerts.clear()

    def record(self, tokens: int):
        self.reset_if_needed()
        self._used += tokens
        self._check_thresholds()

    def _check_thresholds(self):
        for ratio, level, msg in ALERT_THRESHOLDS:
            if self._used / self._limit >= ratio and level not in self._fired_alerts:
                self._fired_alerts.add(level)
                alert = BudgetAlert(level, msg, self._used, self._limit)
                print(f"[ALERT:{level}] {alert.to_json()}")

    def delay_for_headroom(self) -> float:
        self.reset_if_needed()
        ratio = self._used / self._limit
        if ratio >= 1.0:
            return max(1.0, self._reset_at - time.monotonic())
        if ratio >= 0.90:
            return 0.5 * (ratio - 0.90) / 0.10  # 0-0.5s delay as 90-100%
        return 0.0


monitor = HeadroomMonitor()


async def monitored_call(prompt: str) -> str:
    delay = monitor.delay_for_headroom()
    if delay > 0:
        await asyncio.sleep(delay)

    resp = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    )
    tokens = resp.usage.input_tokens + resp.usage.output_tokens
    monitor.record(tokens)
    return resp.content[0].text


async def main():
    prompts = [f"Explain concept #{i} briefly." for i in range(8)]
    results = await asyncio.gather(*[monitored_call(p) for p in prompts])
    print(f"Completed {len(results)} calls")


asyncio.run(main())
```

## Comparison

| Solution | Prediction method | Accuracy | Overhead | Best for |
|---|---|---|---|---|
| **Rolling window tracker** | Usage rate | Medium | Low | General rate limit management |
| **Pre-estimator + queue** | Token estimation | High | Low | Burst workloads |
| **EMA rate predictor** | Exponential smoothing | High | Low | Sustained high-throughput pipelines |
| **Priority budget scheduler** | Per-priority budget | Medium | Medium | Mixed-priority request queues |
| **Adaptive time-of-day scaler** | Historical patterns | High | Low | Predictable usage patterns |
| **Headroom alert + throttle** | Threshold-based | Medium | Zero | Simple alerting + throttle |

Start with **rolling window tracker** (Solution 1) — it handles 90% of cases with minimal code. Add **priority budget scheduler** (Solution 4) when you have mixed-priority workloads. Use **adaptive time-of-day scaler** (Solution 5) when usage follows predictable daily patterns.
