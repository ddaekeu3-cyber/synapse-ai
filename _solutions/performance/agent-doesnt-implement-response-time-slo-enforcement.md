---
title: "Agent Doesn't Implement Response Time SLO Enforcement"
description: "Solutions for enforcing response time Service Level Objectives — automatically applying timeout escalation, model downgrade, and partial result return to keep agents within latency budgets."
tags: [performance, slo, latency, timeout, reliability]
difficulty: intermediate
---

## Problem

Agents often have no latency SLO — they'll wait indefinitely for a slow model response or a hung tool call, burning user patience and downstream timeout budgets. Even when timeouts exist, they're usually a hard crash with no useful output. Agents need graduated latency enforcement: warn early, deliver partial results at the deadline, and auto-downgrade to faster models when the SLO is at risk.

---

## Solution 1: Deadline-Aware Request with Remaining-Time Context

Pass a deadline through the entire request lifecycle. Each component checks remaining time and skips non-critical work as the deadline approaches.

```python
import anthropic
import time
from dataclasses import dataclass
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class Deadline:
    target_ms: int          # Total budget in ms
    start_time: float = 0.0

    def __post_init__(self):
        if self.start_time == 0.0:
            self.start_time = time.time()

    @property
    def elapsed_ms(self) -> int:
        return int((time.time() - self.start_time) * 1000)

    @property
    def remaining_ms(self) -> int:
        return max(0, self.target_ms - self.elapsed_ms)

    @property
    def fraction_remaining(self) -> float:
        return self.remaining_ms / self.target_ms

    @property
    def is_expired(self) -> bool:
        return self.remaining_ms <= 0

    def checkpoint(self, label: str) -> bool:
        """Returns True if enough time remains to continue."""
        remaining = self.remaining_ms
        print(f"  [{label}] elapsed={self.elapsed_ms}ms, remaining={remaining}ms")
        return remaining > 50  # 50ms minimum to do anything useful

def deadline_aware_respond(user_message: str, deadline: Deadline) -> dict:
    if deadline.is_expired:
        return {"response": "Deadline expired before processing.", "partial": True, "elapsed_ms": deadline.elapsed_ms}

    # Select model based on remaining time
    if deadline.fraction_remaining > 0.7:
        model = "claude-sonnet-4-6"
        max_tokens = 1024
    elif deadline.fraction_remaining > 0.4:
        model = "claude-haiku-4-5-20251001"
        max_tokens = 512
    else:
        model = "claude-haiku-4-5-20251001"
        max_tokens = 128  # Very brief response

    deadline.checkpoint("model-selected")

    if not deadline.checkpoint("pre-api-call"):
        return {"response": "Deadline too close — skipping API call.", "partial": True, "elapsed_ms": deadline.elapsed_ms}

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": user_message}],
            timeout=deadline.remaining_ms / 1000,  # pass remaining as timeout
        )
        elapsed = deadline.elapsed_ms
        return {
            "response": response.content[0].text,
            "model": model,
            "elapsed_ms": elapsed,
            "partial": False,
            "slo_met": elapsed <= deadline.target_ms,
        }
    except Exception as e:
        return {
            "response": f"Request failed within deadline: {e.__class__.__name__}",
            "elapsed_ms": deadline.elapsed_ms,
            "partial": True,
        }

# Tight deadline (500ms) — forces haiku + reduced tokens
print("=== Tight deadline (500ms) ===")
deadline = Deadline(target_ms=500)
result = deadline_aware_respond("Explain quantum computing in detail with examples.", deadline)
print(f"Model: {result.get('model')} | SLO met: {result.get('slo_met')} | Elapsed: {result.get('elapsed_ms')}ms")

# Comfortable deadline (5000ms) — uses sonnet
print("\n=== Comfortable deadline (5000ms) ===")
deadline2 = Deadline(target_ms=5000)
result2 = deadline_aware_respond("What is machine learning?", deadline2)
print(f"Model: {result2.get('model')} | SLO met: {result2.get('slo_met')} | Elapsed: {result2.get('elapsed_ms')}ms")
```

---

## Solution 2: Speculative Fast Path with Slow Path Fallback

Issue a fast (haiku) request immediately and a thorough (sonnet/opus) request concurrently. Return whichever completes within the SLO window.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass
from typing import Optional

client = anthropic.AsyncAnthropic()

@dataclass
class PathResult:
    model: str
    response: str
    latency_ms: int
    path: str  # "fast" or "slow"

async def fast_path(message: str) -> PathResult:
    t0 = time.time()
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": message}],
    )
    return PathResult(
        model="claude-haiku-4-5-20251001",
        response=response.content[0].text,
        latency_ms=int((time.time() - t0) * 1000),
        path="fast",
    )

async def slow_path(message: str) -> PathResult:
    t0 = time.time()
    response = await client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        messages=[{"role": "user", "content": message}],
    )
    return PathResult(
        model="claude-sonnet-4-6",
        response=response.content[0].text,
        latency_ms=int((time.time() - t0) * 1000),
        path="slow",
    )

async def speculative_respond(
    message: str,
    slo_target_ms: int = 1000,
) -> dict:
    """
    Race fast path against slow path.
    If fast path completes within SLO window, return it.
    Otherwise upgrade to slow path result.
    """
    t0 = time.time()

    fast_task = asyncio.create_task(fast_path(message))
    slow_task = asyncio.create_task(slow_path(message))

    result = None
    winner = None

    # Wait for fast path up to SLO - 100ms buffer
    try:
        fast_result = await asyncio.wait_for(
            asyncio.shield(fast_task),
            timeout=(slo_target_ms - 100) / 1000
        )
        elapsed = int((time.time() - t0) * 1000)
        if elapsed <= slo_target_ms:
            result = fast_result
            winner = "fast"
            # Cancel slow path if fast was good enough
            slow_task.cancel()
        else:
            winner = "slow (fast missed SLO)"
    except asyncio.TimeoutError:
        # Fast path is too slow — wait for slow path
        winner = "slow (fast timed out)"

    if result is None:
        try:
            result = await asyncio.wait_for(slow_task, timeout=30)
        except Exception:
            # If slow also fails, return whatever fast produced
            try:
                result = await fast_task
                winner = "fast (fallback)"
            except Exception as e:
                return {"error": str(e), "elapsed_ms": int((time.time() - t0) * 1000)}

    total_ms = int((time.time() - t0) * 1000)
    return {
        "response": result.response,
        "model": result.model,
        "path_winner": winner,
        "path_latency_ms": result.latency_ms,
        "total_latency_ms": total_ms,
        "slo_met": total_ms <= slo_target_ms,
        "slo_target_ms": slo_target_ms,
    }

async def main():
    for slo in [800, 3000]:
        print(f"\n=== SLO target: {slo}ms ===")
        result = await speculative_respond(
            "Explain the key benefits of using async Python for I/O-bound tasks.",
            slo_target_ms=slo,
        )
        print(f"Winner: {result['path_winner']} | Model: {result['model']}")
        print(f"Total: {result['total_latency_ms']}ms | SLO met: {result['slo_met']}")
        print(f"Response: {result['response'][:80]}...")

asyncio.run(main())
```

---

## Solution 3: Latency Percentile Budget with Automatic Throttling

Track actual latency percentiles per model and automatically throttle to faster models when p95 latency exceeds SLO.

```python
import anthropic
import time
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class LatencyRecord:
    model: str
    latency_ms: int
    timestamp: float

class LatencyBudgetEnforcer:
    def __init__(
        self,
        p95_slo_ms: int = 2000,
        window_seconds: int = 300,
        violation_threshold: float = 0.05,  # Allow 5% violations
    ):
        self._slo_ms = p95_slo_ms
        self._window = window_seconds
        self._violation_threshold = violation_threshold
        self._records: deque[LatencyRecord] = deque(maxlen=1000)
        self._current_model = "claude-sonnet-4-6"
        self._degraded = False

    def _current_window(self) -> list[LatencyRecord]:
        cutoff = time.time() - self._window
        return [r for r in self._records if r.timestamp >= cutoff]

    def _p95(self, records: list[LatencyRecord]) -> Optional[float]:
        if len(records) < 10:
            return None
        latencies = sorted(r.latency_ms for r in records)
        idx = int(len(latencies) * 0.95)
        return latencies[min(idx, len(latencies) - 1)]

    def _violation_rate(self, records: list[LatencyRecord]) -> float:
        if not records:
            return 0.0
        violations = sum(1 for r in records if r.latency_ms > self._slo_ms)
        return violations / len(records)

    def _evaluate_and_adapt(self):
        records = self._current_window()
        if len(records) < 5:
            return

        p95 = self._p95(records)
        violation_rate = self._violation_rate(records)

        if p95 and p95 > self._slo_ms and violation_rate > self._violation_threshold:
            if not self._degraded:
                print(f"[SLO] p95={p95:.0f}ms > {self._slo_ms}ms SLO. "
                      f"Violation rate={violation_rate:.0%}. Downgrading to haiku.")
                self._current_model = "claude-haiku-4-5-20251001"
                self._degraded = True
        elif self._degraded:
            if p95 and p95 <= self._slo_ms * 0.8 and violation_rate < self._violation_threshold * 0.5:
                print(f"[SLO] p95={p95:.0f}ms recovered. Restoring sonnet.")
                self._current_model = "claude-sonnet-4-6"
                self._degraded = False

    def call(self, messages: list, max_tokens: int = 512, **kwargs) -> dict:
        self._evaluate_and_adapt()
        model = self._current_model

        t0 = time.time()
        response = client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages, **kwargs
        )
        latency_ms = int((time.time() - t0) * 1000)

        self._records.append(LatencyRecord(
            model=model, latency_ms=latency_ms, timestamp=time.time()
        ))

        return {
            "response": response.content[0].text,
            "model": model,
            "latency_ms": latency_ms,
            "slo_met": latency_ms <= self._slo_ms,
            "degraded": self._degraded,
        }

    def stats(self) -> dict:
        records = self._current_window()
        p95 = self._p95(records)
        return {
            "model": self._current_model,
            "degraded": self._degraded,
            "p95_latency_ms": round(p95, 0) if p95 else None,
            "slo_ms": self._slo_ms,
            "violation_rate": round(self._violation_rate(records), 3),
            "sample_count": len(records),
        }

enforcer = LatencyBudgetEnforcer(p95_slo_ms=1500, window_seconds=300)

# Simulate calls
for prompt in [
    "What is 2+2?",
    "List 3 Python tips.",
    "What is ML?",
]:
    result = enforcer.call([{"role": "user", "content": prompt}])
    print(f"[{result['model'][:15]}] {result['latency_ms']}ms {'✓' if result['slo_met'] else '✗'}")

print(f"\nStats: {enforcer.stats()}")
```

---

## Solution 4: Multi-Stage Timeout with Partial Result Return

Apply increasing pressure at defined checkpoints: warn at 50%, degrade at 75%, return partial at 90%, hard-stop at 100%.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass
from typing import Optional

client = anthropic.AsyncAnthropic()

@dataclass
class SLOCheckpoint:
    name: str
    pct: float
    action: str  # warn, degrade, partial, terminate

CHECKPOINTS = [
    SLOCheckpoint("warn",      0.50, "warn"),
    SLOCheckpoint("degrade",   0.75, "degrade"),
    SLOCheckpoint("partial",   0.90, "partial"),
    SLOCheckpoint("terminate", 1.00, "terminate"),
]

async def multi_stage_call(
    messages: list,
    slo_ms: int = 3000,
    model_primary: str = "claude-sonnet-4-6",
    model_fallback: str = "claude-haiku-4-5-20251001",
) -> dict:
    t0 = time.time()
    partial_result = None
    actions_taken = []
    current_model = model_primary

    async def get_response(model: str, max_tok: int = 512) -> Optional[str]:
        try:
            resp = await asyncio.wait_for(
                client.messages.create(
                    model=model, max_tokens=max_tok,
                    messages=messages,
                ),
                timeout=(slo_ms - int((time.time() - t0) * 1000)) / 1000
            )
            return resp.content[0].text
        except asyncio.TimeoutError:
            return None

    # Stage 1: Start primary model call
    primary_task = asyncio.create_task(get_response(current_model, 1024))

    # Monitor checkpoints
    for checkpoint in CHECKPOINTS:
        wait_ms = slo_ms * checkpoint.pct - (time.time() - t0) * 1000
        if wait_ms > 0:
            done, _ = await asyncio.wait([primary_task], timeout=wait_ms / 1000)
            if done:
                break  # Primary completed before checkpoint

        elapsed_ms = int((time.time() - t0) * 1000)
        frac = elapsed_ms / slo_ms

        if frac >= checkpoint.pct and not primary_task.done():
            actions_taken.append(checkpoint.action)

            if checkpoint.action == "warn":
                print(f"  [50%] Slow response warning at {elapsed_ms}ms")

            elif checkpoint.action == "degrade":
                print(f"  [75%] Degrading to fallback model at {elapsed_ms}ms")
                primary_task.cancel()
                primary_task = asyncio.create_task(get_response(model_fallback, 256))
                current_model = model_fallback

            elif checkpoint.action == "partial":
                print(f"  [90%] Requesting partial result at {elapsed_ms}ms")
                partial_task = asyncio.create_task(
                    client.messages.create(
                        model=model_fallback, max_tokens=64,
                        messages=[{"role": "user", "content":
                                   f"In ONE sentence: {messages[-1]['content'][:100]}"}]
                    )
                )
                try:
                    partial_resp = await asyncio.wait_for(partial_task, timeout=0.5)
                    partial_result = partial_resp.content[0].text
                except Exception:
                    partial_result = "Partial: Processing timed out."

            elif checkpoint.action == "terminate":
                print(f"  [100%] SLO exceeded at {elapsed_ms}ms — returning partial")
                primary_task.cancel()
                return {
                    "response": partial_result or "Request timed out.",
                    "model": current_model,
                    "elapsed_ms": elapsed_ms,
                    "slo_met": False,
                    "partial": True,
                    "actions": actions_taken,
                }

    # Collect primary result
    try:
        result = await primary_task
    except (asyncio.CancelledError, Exception):
        result = None

    elapsed_ms = int((time.time() - t0) * 1000)
    return {
        "response": result or partial_result or "No result obtained.",
        "model": current_model,
        "elapsed_ms": elapsed_ms,
        "slo_met": elapsed_ms <= slo_ms,
        "partial": result is None,
        "actions": actions_taken,
    }

async def main():
    msg = [{"role": "user", "content": "Write a detailed explanation of Transformer architecture."}]

    for slo in [500, 5000]:
        print(f"\n=== SLO: {slo}ms ===")
        result = await multi_stage_call(msg, slo_ms=slo)
        print(f"Model: {result['model']} | Elapsed: {result['elapsed_ms']}ms | SLO met: {result['slo_met']}")
        print(f"Actions: {result['actions']}")
        print(f"Response: {result['response'][:80]}...")

asyncio.run(main())
```

---

## Solution 5: Per-User Latency SLO with Priority Queuing

Assign latency SLOs per user tier and maintain a priority queue that ensures premium users always get responses within their SLO.

```python
import anthropic
import asyncio
import time
import heapq
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.AsyncAnthropic()

USER_TIERS = {
    "enterprise": {"slo_ms": 1000, "max_tokens": 2048, "model": "claude-sonnet-4-6",    "priority": 0},
    "pro":        {"slo_ms": 2000, "max_tokens": 1024, "model": "claude-haiku-4-5-20251001", "priority": 1},
    "free":       {"slo_ms": 5000, "max_tokens": 256,  "model": "claude-haiku-4-5-20251001", "priority": 2},
}

@dataclass(order=True)
class QueuedRequest:
    priority: int
    created_at: float
    request_id: str = field(compare=False)
    user_id: str = field(compare=False)
    tier: str = field(compare=False)
    message: str = field(compare=False)
    future: asyncio.Future = field(compare=False)

class PrioritySLOQueue:
    def __init__(self, workers: int = 3):
        self._heap: list[QueuedRequest] = []
        self._workers = workers
        self._semaphore = asyncio.Semaphore(workers)
        self._latency_log: list[dict] = []

    async def enqueue(self, user_id: str, tier: str, message: str, request_id: str) -> dict:
        tier_config = USER_TIERS.get(tier, USER_TIERS["free"])
        future: asyncio.Future = asyncio.get_event_loop().create_future()

        req = QueuedRequest(
            priority=tier_config["priority"],
            created_at=time.time(),
            request_id=request_id,
            user_id=user_id,
            tier=tier,
            message=message,
            future=future,
        )
        heapq.heappush(self._heap, req)
        asyncio.create_task(self._process_next())
        return await future

    async def _process_next(self):
        async with self._semaphore:
            if not self._heap:
                return
            req = heapq.heappop(self._heap)
            t0 = time.time()
            tier_config = USER_TIERS[req.tier]

            # Check if SLO already violated by queue wait
            queue_wait_ms = int((t0 - req.created_at) * 1000)
            slo_ms = tier_config["slo_ms"]

            if queue_wait_ms > slo_ms * 0.5:
                print(f"  [SLO Risk] {req.user_id} ({req.tier}) waited {queue_wait_ms}ms in queue")

            try:
                response = await asyncio.wait_for(
                    client.messages.create(
                        model=tier_config["model"],
                        max_tokens=tier_config["max_tokens"],
                        messages=[{"role": "user", "content": req.message}],
                    ),
                    timeout=(slo_ms - queue_wait_ms) / 1000,
                )
                total_ms = int((time.time() - req.created_at) * 1000)
                result = {
                    "response": response.content[0].text,
                    "model": tier_config["model"],
                    "total_ms": total_ms,
                    "slo_met": total_ms <= slo_ms,
                    "queue_wait_ms": queue_wait_ms,
                }
            except asyncio.TimeoutError:
                total_ms = int((time.time() - req.created_at) * 1000)
                result = {
                    "response": "Response timed out — SLO exceeded.",
                    "total_ms": total_ms,
                    "slo_met": False,
                    "queue_wait_ms": queue_wait_ms,
                }

            self._latency_log.append({"user": req.user_id, "tier": req.tier, **result})
            req.future.set_result(result)

    def latency_report(self) -> dict:
        by_tier: dict[str, list] = {}
        for entry in self._latency_log:
            by_tier.setdefault(entry["tier"], []).append(entry["total_ms"])
        return {
            tier: {
                "avg_ms": round(sum(ms)/len(ms), 0),
                "max_ms": max(ms),
                "count": len(ms),
            }
            for tier, ms in by_tier.items()
        }

async def main():
    queue = PrioritySLOQueue(workers=3)

    # Mix of users from different tiers
    requests = [
        ("user-1", "free",       "What is Python?"),
        ("user-2", "enterprise", "Explain ML."),
        ("user-3", "pro",        "What is async?"),
        ("user-4", "free",       "What is a list?"),
        ("user-5", "enterprise", "What is a decorator?"),
    ]

    tasks = [
        queue.enqueue(uid, tier, msg, f"req-{i}")
        for i, (uid, tier, msg) in enumerate(requests)
    ]
    results = await asyncio.gather(*tasks)

    print("=== Results ===")
    for i, (r, (uid, tier, _)) in enumerate(zip(results, requests)):
        slo_ms = USER_TIERS[tier]["slo_ms"]
        print(f"  {uid} ({tier}): {r['total_ms']}ms / {slo_ms}ms SLO — {'✓' if r['slo_met'] else '✗'}")

    print(f"\nLatency report: {queue.latency_report()}")

asyncio.run(main())
```

---

## Solution 6: Adaptive SLO Tightening Based on Success Trends

Automatically tighten or relax latency SLOs based on observed performance trends — raising the bar when the system is fast, loosening during degradation.

```python
import anthropic
import time
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

client = anthropic.Anthropic()

@dataclass
class AdaptiveSLO:
    base_slo_ms: int = 2000      # Initial SLO
    min_slo_ms: int = 800        # Never go below this (tightest)
    max_slo_ms: int = 5000       # Never go above this (loosest)
    adjustment_factor: float = 0.1  # 10% per adjustment
    window_seconds: int = 300
    _latencies: deque = field(default_factory=lambda: deque(maxlen=500))
    _current_slo_ms: int = 0

    def __post_init__(self):
        self._current_slo_ms = self.base_slo_ms

    @property
    def current_slo_ms(self) -> int:
        return self._current_slo_ms

    def record(self, latency_ms: int):
        self._latencies.append((time.time(), latency_ms))
        self._maybe_adjust()

    def _current_window_latencies(self) -> list[int]:
        cutoff = time.time() - self.window_seconds
        return [ms for t, ms in self._latencies if t >= cutoff]

    def _p95(self, values: list[int]) -> Optional[float]:
        if len(values) < 10:
            return None
        sorted_v = sorted(values)
        return sorted_v[int(len(sorted_v) * 0.95)]

    def _maybe_adjust(self):
        if len(self._latencies) % 20 != 0:  # Adjust every 20 records
            return
        latencies = self._current_window_latencies()
        p95 = self._p95(latencies)
        if p95 is None:
            return

        old_slo = self._current_slo_ms
        # If p95 is comfortably below SLO: tighten
        if p95 < self._current_slo_ms * 0.6:
            new_slo = int(self._current_slo_ms * (1 - self.adjustment_factor))
            self._current_slo_ms = max(self.min_slo_ms, new_slo)
        # If p95 is above SLO: relax
        elif p95 > self._current_slo_ms:
            new_slo = int(self._current_slo_ms * (1 + self.adjustment_factor))
            self._current_slo_ms = min(self.max_slo_ms, new_slo)

        if self._current_slo_ms != old_slo:
            direction = "↓ tightened" if self._current_slo_ms < old_slo else "↑ relaxed"
            print(f"  [Adaptive SLO] {old_slo}ms → {self._current_slo_ms}ms {direction} (p95={p95:.0f}ms)")

class AdaptiveSLOAgent:
    def __init__(self):
        self._slo = AdaptiveSLO(base_slo_ms=2000)

    def respond(self, message: str) -> dict:
        t0 = time.time()
        model = "claude-haiku-4-5-20251001"

        response = client.messages.create(
            model=model, max_tokens=256,
            messages=[{"role": "user", "content": message}],
            timeout=self._slo.current_slo_ms / 1000,
        )
        latency_ms = int((time.time() - t0) * 1000)
        self._slo.record(latency_ms)

        return {
            "response": response.content[0].text,
            "latency_ms": latency_ms,
            "current_slo_ms": self._slo.current_slo_ms,
            "slo_met": latency_ms <= self._slo.current_slo_ms,
        }

agent = AdaptiveSLOAgent()
prompts = [
    "What is 2+2?",
    "Define ML.",
    "What is Python?",
    "Name 3 colors.",
    "What is HTTP?",
]

print(f"Initial SLO: {agent._slo.current_slo_ms}ms\n")
for i, p in enumerate(prompts * 6):  # 30 calls
    result = agent.respond(p)
    if i % 10 == 0:
        print(f"[Call {i+1}] latency={result['latency_ms']}ms | SLO={result['current_slo_ms']}ms")

print(f"\nFinal adaptive SLO: {agent._slo.current_slo_ms}ms")
```

---

## Comparison

| Solution | SLO Type | Partial Results | Auto-Adaptation | Async-Ready | Best For |
|---|---|---|---|---|---|
| Deadline-Aware Request | Absolute deadline | No (fail fast) | Model selection | No | Simple timeout enforcement |
| Speculative Fast/Slow Path | P50 / P95 split | No | Race-based | Yes | Latency-sensitive endpoints |
| Latency Percentile + Throttle | p95 SLO | No | Model downgrade | No | Statistical SLO tracking |
| Multi-Stage Timeout | Absolute + checkpoints | Yes | Graduated response | Yes | Critical user-facing flows |
| Priority Queue by Tier | Per-tier SLO | No | Priority-based | Yes | Multi-tenant services |
| Adaptive SLO Tightening | Self-adjusting | No | Yes (data-driven) | No | Long-running stable services |

**Recommended approach:** Deploy Solution 1 (deadline-aware) as the foundation for all calls, Solution 3 (latency percentile tracker) for continuous p95 monitoring, and Solution 4 (multi-stage timeout) for user-facing APIs where partial results are better than errors. Add Solution 5 (priority queue) for multi-tenant services with different SLA tiers.
