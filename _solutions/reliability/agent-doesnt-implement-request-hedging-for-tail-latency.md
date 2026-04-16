---
title: "Agent Doesn't Implement Request Hedging for Tail Latency"
description: "Fire duplicate requests to multiple endpoints simultaneously, use the first response, and cancel the rest — eliminating p99 latency spikes."
category: reliability
difficulty: intermediate
tags: [latency, hedging, reliability, asyncio, performance, tail-latency]
---

# Agent Doesn't Implement Request Hedging for Tail Latency

## Problem

LLM API latency is highly variable. The p50 might be 800ms but the p99 is 8 seconds. When agents call a single endpoint, a single slow server causes cascading delays. Request hedging fires the same request to multiple endpoints simultaneously and uses whichever responds first — trading marginal extra cost for dramatically lower tail latency.

---

## Option 1: Simple Speculative Hedge After Deadline

```python
import asyncio
import anthropic
import time

client = anthropic.AsyncAnthropic()

async def hedged_call(
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
    hedge_after_ms: float = 600.0,
    max_hedges: int = 2,
) -> tuple[str, int]:
    """
    Returns (response_text, attempt_index_that_won).
    Fires a hedge if primary doesn't respond within hedge_after_ms.
    """
    winner_text: list[str] = []
    winner_idx: list[int] = []
    done_event = asyncio.Event()
    tasks: list[asyncio.Task] = []

    async def attempt(idx: int, delay_ms: float):
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        if done_event.is_set():
            return
        try:
            t0 = time.time()
            resp = await client.messages.create(
                model=model, max_tokens=max_tokens, messages=messages
            )
            if not done_event.is_set():
                done_event.set()
                winner_text.append(resp.content[0].text)
                winner_idx.append(idx)
                latency = (time.time() - t0) * 1000
                if idx > 0:
                    print(f"[HEDGE] Attempt {idx} won at {latency:.0f}ms")
        except Exception as e:
            if idx == 0:
                print(f"[HEDGE] Primary failed: {e}")

    # Fire primary immediately, hedges after staggered delays
    for i in range(max_hedges + 1):
        task = asyncio.create_task(attempt(i, i * hedge_after_ms))
        tasks.append(task)

    await done_event.wait()

    # Cancel all remaining tasks
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if not winner_text:
        raise RuntimeError("All hedged attempts failed")

    return winner_text[0], winner_idx[0]

async def main():
    msgs = [{"role": "user", "content": "What is request hedging?"}]
    t0 = time.time()
    text, winner = await hedged_call(msgs, hedge_after_ms=500.0, max_hedges=2)
    print(f"Winner: attempt {winner}, total={((time.time()-t0)*1000):.0f}ms")
    print(text[:150])

asyncio.run(main())
```

---

## Option 2: Adaptive Hedge Threshold Based on Historical Latency

```python
import asyncio
import anthropic
import time
import statistics
from dataclasses import dataclass, field
from collections import deque

client = anthropic.AsyncAnthropic()

@dataclass
class LatencyStats:
    samples: deque = field(default_factory=lambda: deque(maxlen=50))
    hedge_percentile: float = 0.75  # hedge if exceeds 75th percentile

    def record(self, ms: float):
        self.samples.append(ms)

    def hedge_threshold_ms(self) -> float:
        if len(self.samples) < 5:
            return 800.0  # default before enough data
        sorted_s = sorted(self.samples)
        idx = int(len(sorted_s) * self.hedge_percentile)
        return sorted_s[min(idx, len(sorted_s) - 1)]

    def p99_ms(self) -> float:
        if not self.samples:
            return 0.0
        sorted_s = sorted(self.samples)
        return sorted_s[int(len(sorted_s) * 0.99)]

stats = LatencyStats()

async def adaptive_hedged_call(messages: list[dict], model: str = "claude-sonnet-4-6", max_tokens: int = 512) -> str:
    threshold_ms = stats.hedge_threshold_ms()
    done = asyncio.Event()
    result: list[str] = []
    tasks: list[asyncio.Task] = []

    async def fire(delay_ms: float, label: str):
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        if done.is_set():
            return
        t0 = time.time()
        try:
            resp = await client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
            elapsed = (time.time() - t0) * 1000
            if not done.is_set():
                done.set()
                result.append(resp.content[0].text)
                stats.record(elapsed)
                if label != "primary":
                    print(f"[ADAPTIVE HEDGE] {label} won (threshold was {threshold_ms:.0f}ms)")
        except Exception as e:
            print(f"[ADAPTIVE HEDGE] {label} failed: {e}")

    tasks.append(asyncio.create_task(fire(0, "primary")))
    tasks.append(asyncio.create_task(fire(threshold_ms, "hedge-1")))
    tasks.append(asyncio.create_task(fire(threshold_ms * 1.5, "hedge-2")))

    await done.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if not result:
        raise RuntimeError("All hedge attempts failed")
    return result[0]

async def main():
    # Warm up stats
    for q in ["ping", "hello", "test"]:
        t0 = time.time()
        await client.messages.create(model="claude-haiku-4-5-20251001", max_tokens=5, messages=[{"role": "user", "content": q}])
        stats.record((time.time() - t0) * 1000)

    print(f"Hedge threshold: {stats.hedge_threshold_ms():.0f}ms")
    result = await adaptive_hedged_call([{"role": "user", "content": "Explain tail latency."}])
    print(result[:150])

asyncio.run(main())
```

---

## Option 3: Cross-Model Hedging (Different Models as Backups)

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass

client = anthropic.AsyncAnthropic()

@dataclass
class HedgeTarget:
    model: str
    delay_ms: float
    label: str
    quality_weight: float = 1.0  # higher = prefer this model's result

async def cross_model_hedge(
    messages: list[dict],
    targets: list[HedgeTarget],
    max_tokens: int = 512,
    prefer_quality: bool = False,
) -> tuple[str, str]:
    """
    prefer_quality: if True, wait for highest-weight model that completes within 2x primary latency.
    Returns (response_text, model_label).
    """
    results: list[tuple[float, float, str, str]] = []  # (arrival_time, quality_weight, text, label)
    first_arrival = asyncio.Event()
    lock = asyncio.Lock()

    async def fire(target: HedgeTarget):
        await asyncio.sleep(target.delay_ms / 1000.0)
        try:
            t0 = time.time()
            resp = await client.messages.create(
                model=target.model, max_tokens=max_tokens, messages=messages
            )
            latency = time.time() - t0
            async with lock:
                results.append((time.time(), target.quality_weight, resp.content[0].text, target.label))
                first_arrival.set()
        except Exception as e:
            print(f"[CROSS-HEDGE] {target.label} failed: {e}")

    tasks = [asyncio.create_task(fire(t)) for t in targets]
    await first_arrival.wait()

    if prefer_quality:
        # Wait a bit more to see if a higher-quality model arrives
        await asyncio.sleep(0.5)

    # Cancel remaining
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if not results:
        raise RuntimeError("All hedge targets failed")

    if prefer_quality:
        # Pick highest quality among arrived results
        best = max(results, key=lambda r: r[1])
    else:
        # Pick first arrived
        best = min(results, key=lambda r: r[0])

    return best[2], best[3]

async def main():
    targets = [
        HedgeTarget(model="claude-sonnet-4-6", delay_ms=0, label="sonnet", quality_weight=1.0),
        HedgeTarget(model="claude-haiku-4-5-20251001", delay_ms=400, label="haiku-hedge", quality_weight=0.7),
        HedgeTarget(model="claude-sonnet-4-6", delay_ms=700, label="sonnet-hedge-2", quality_weight=1.0),
    ]
    text, label = await cross_model_hedge(
        [{"role": "user", "content": "What is p99 latency?"}],
        targets=targets
    )
    print(f"[{label}] {text[:150]}")

asyncio.run(main())
```

---

## Option 4: Probabilistic Hedging with Cost Budget

```python
import asyncio
import anthropic
import time
import random
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

@dataclass
class HedgeBudget:
    max_hedge_rate: float = 0.15    # hedge at most 15% of requests
    hedge_cost_multiplier: float = 1.8  # hedging costs ~1.8x per hedged request
    requests_sent: int = 0
    hedges_fired: int = 0

    def should_hedge(self) -> bool:
        if self.requests_sent == 0:
            return False
        current_rate = self.hedges_fired / self.requests_sent
        return current_rate < self.max_hedge_rate and random.random() < 0.5

    def record(self, hedged: bool):
        self.requests_sent += 1
        if hedged:
            self.hedges_fired += 1

    def hedge_rate(self) -> float:
        return self.hedges_fired / self.requests_sent if self.requests_sent else 0.0

budget = HedgeBudget(max_hedge_rate=0.20)

async def budget_hedged_call(
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
    hedge_delay_ms: float = 500.0,
) -> str:
    should_hedge = budget.should_hedge()
    budget.record(should_hedge)

    if not should_hedge:
        # Standard call, no hedge
        resp = await client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
        return resp.content[0].text

    # Hedge
    done = asyncio.Event()
    result: list[str] = []

    async def fire(delay_ms: float, label: str):
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        if done.is_set():
            return
        try:
            resp = await client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
            if not done.is_set():
                done.set()
                result.append(resp.content[0].text)
                if label != "primary":
                    print(f"[BUDGET HEDGE] hedge won")
        except Exception:
            pass

    tasks = [
        asyncio.create_task(fire(0, "primary")),
        asyncio.create_task(fire(hedge_delay_ms, "hedge")),
    ]
    await done.wait()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    return result[0] if result else ""

async def main():
    msgs = [{"role": "user", "content": "What is hedging?"}]
    for i in range(10):
        await budget_hedged_call(msgs)
    print(f"Hedge rate: {budget.hedge_rate():.1%} ({budget.hedges_fired}/{budget.requests_sent})")

asyncio.run(main())
```

---

## Option 5: Streaming Hedge — First Token Wins

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

async def streaming_hedge(
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 1024,
    hedge_delay_ms: float = 600.0,
) -> str:
    """Race streaming responses — stream first-token winner, cancel the rest."""
    winner_stream_content: list[str] = []
    first_token_event = asyncio.Event()
    lock = asyncio.Lock()

    async def stream_attempt(delay_ms: float, label: str):
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        if first_token_event.is_set():
            return

        try:
            full_text = []
            async with client.messages.stream(
                model=model, max_tokens=max_tokens, messages=messages
            ) as stream:
                async for text_chunk in stream.text_stream:
                    async with lock:
                        if not first_token_event.is_set():
                            # This stream won — take ownership
                            first_token_event.set()
                            print(f"[STREAM HEDGE] {label} won first token")
                        elif label not in [l for l in winner_stream_content[:1]]:
                            # Another stream already won
                            return
                    full_text.append(text_chunk)
                    print(text_chunk, end="", flush=True)

                if full_text:
                    winner_stream_content.extend(full_text)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            if not first_token_event.is_set():
                print(f"[STREAM HEDGE] {label} error: {e}")

    tasks = [
        asyncio.create_task(stream_attempt(0, "primary")),
        asyncio.create_task(stream_attempt(hedge_delay_ms, "hedge")),
    ]

    await first_token_event.wait()
    # Give the winning stream time to complete
    await asyncio.sleep(30)  # generous timeout for completion

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    print()  # newline after stream

    return "".join(winner_stream_content)

async def main():
    t0 = time.time()
    result = await streaming_hedge(
        [{"role": "user", "content": "Explain streaming latency in 2 sentences."}],
        hedge_delay_ms=500.0
    )
    print(f"\nTotal: {(time.time()-t0)*1000:.0f}ms, {len(result)} chars")

asyncio.run(main())
```

---

## Option 6: Hedge with Cancellation Token and Observability

```python
import asyncio
import anthropic
import time
from dataclasses import dataclass, field
from collections import defaultdict

client = anthropic.AsyncAnthropic()

@dataclass
class HedgeMetrics:
    total_requests: int = 0
    hedges_won: int = 0
    primaries_won: int = 0
    both_failed: int = 0
    hedge_latency_savings_ms: list = field(default_factory=list)

    def report(self) -> dict:
        avg_saving = sum(self.hedge_latency_savings_ms) / len(self.hedge_latency_savings_ms) if self.hedge_latency_savings_ms else 0.0
        return {
            "total": self.total_requests,
            "primary_wins": self.primaries_won,
            "hedge_wins": self.hedges_won,
            "failures": self.both_failed,
            "avg_latency_saving_ms": round(avg_saving, 1),
        }

metrics = HedgeMetrics()

@dataclass
class HedgeResult:
    text: str
    winner: str  # "primary" | "hedge-N"
    winner_latency_ms: float
    loser_latency_ms: float | None = None

async def observable_hedge(
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
    hedge_delay_ms: float = 500.0,
) -> HedgeResult:
    metrics.total_requests += 1
    results: list[tuple[float, str, str]] = []  # (latency_ms, label, text)
    done = asyncio.Event()
    lock = asyncio.Lock()
    start = time.time()

    async def fire(delay_ms: float, label: str):
        if delay_ms > 0:
            await asyncio.sleep(delay_ms / 1000.0)
        if done.is_set():
            return
        t0 = time.time()
        try:
            resp = await client.messages.create(model=model, max_tokens=max_tokens, messages=messages)
            elapsed = (time.time() - t0) * 1000
            async with lock:
                results.append((elapsed, label, resp.content[0].text))
                done.set()
        except Exception as e:
            async with lock:
                results.append((9999, label, f"ERROR: {e}"))

    tasks = [
        asyncio.create_task(fire(0, "primary")),
        asyncio.create_task(fire(hedge_delay_ms, "hedge-1")),
    ]

    await done.wait()
    # Brief wait for the loser's latency measurement
    await asyncio.sleep(0.05)

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if not results:
        metrics.both_failed += 1
        raise RuntimeError("All hedge attempts failed")

    # Winner = first to arrive (smallest latency)
    winner_latency, winner_label, winner_text = min(results, key=lambda r: r[0])
    loser_latency = max(r[0] for r in results) if len(results) > 1 else None

    if winner_label == "primary":
        metrics.primaries_won += 1
    else:
        metrics.hedges_won += 1
        if loser_latency:
            metrics.hedge_latency_savings_ms.append(loser_latency - winner_latency)

    return HedgeResult(
        text=winner_text,
        winner=winner_label,
        winner_latency_ms=winner_latency,
        loser_latency_ms=loser_latency
    )

async def main():
    for i in range(5):
        result = await observable_hedge(
            [{"role": "user", "content": f"Question {i}: What is latency?"}],
            hedge_delay_ms=400.0
        )
        print(f"[{result.winner} {result.winner_latency_ms:.0f}ms] {result.text[:60]}...")

    print(f"\nMetrics: {metrics.report()}")

asyncio.run(main())
```

---

## Comparison

| Option | Hedge Trigger | Model Diversity | Cost Control | Best For |
|--------|-------------|----------------|-------------|----------|
| 1 – Fixed Deadline | After N ms | Same model | None | Simple tail-latency reduction |
| 2 – Adaptive Threshold | After p75 of history | Same model | Automatic (data-driven) | Production with varying load |
| 3 – Cross-Model | After N ms | Different models | Quality preference | Quality/latency trade-off |
| 4 – Probabilistic Budget | Random, budget-gated | Same model | Max hedge rate cap | Cost-sensitive production |
| 5 – Streaming First-Token | After N ms | Same model | None | Streaming UX optimization |
| 6 – Observable | After N ms | Same model | Metrics-driven tuning | Observability-first production |

**Recommendation:** Use Option 2 (adaptive threshold) in production — it automatically calibrates the hedge delay to your actual latency distribution, so you hedge exactly when it helps. Add Option 6's metrics to track hedge wins and tune your threshold. Use Option 3 when you want the hedge to also serve as a quality fallback.
