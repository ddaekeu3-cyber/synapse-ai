---
layout: solution
title: "Agent Doesn't Implement Request Hedging for Latency Reduction"
category: performance
description: "When agents wait on a single slow API call, tail latency spikes hurt user experience. Request hedging sends speculative duplicate requests and uses whichever responds first, cancelling the rest."
tags: [hedging, latency, performance, speculative-execution, asyncio, tail-latency]
---

# Agent Doesn't Implement Request Hedging for Latency Reduction

## The Problem

LLM API latency follows a heavy-tailed distribution: the median response may be 800ms, but p99 can exceed 8 seconds. A single slow call blocks the entire pipeline. Request hedging (also called speculative execution) solves this by sending 2+ identical requests and returning the first response that arrives, cancelling the losers.

The tradeoff: hedging roughly doubles token cost but can cut p99 latency by 60-80%. For user-facing, latency-sensitive agents, this is often the right trade.

---

## Option 1: Simple Dual-Request Race

Fire two identical requests simultaneously and return whichever responds first.

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

async def hedged_request(
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512
) -> dict:
    """Send two identical requests; return the first to complete."""
    start = time.monotonic()

    async def make_request(replica_id: int) -> dict:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages
        )
        return {
            "replica_id": replica_id,
            "text": response.content[0].text,
            "latency_ms": int((time.monotonic() - start) * 1000)
        }

    # Race both requests
    tasks = [
        asyncio.create_task(make_request(0)),
        asyncio.create_task(make_request(1))
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    # Cancel the slower request
    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    result = done.pop().result()
    result["hedged"] = True
    result["replicas_sent"] = 2
    return result

async def main():
    messages = [{"role": "user", "content": "Explain neural networks in 3 sentences."}]

    start = time.monotonic()
    result = await hedged_request(messages)
    total_ms = int((time.monotonic() - start) * 1000)

    print(f"Winner: replica {result['replica_id']}")
    print(f"Response latency: {result['latency_ms']}ms")
    print(f"Wall time: {total_ms}ms")
    print(f"Response: {result['text'][:200]}")

asyncio.run(main())

# Expected Token Savings: Doubles token cost but cuts p99 latency 50-70%; net positive for latency-SLA-bound services
# Environment: user-facing chatbots, real-time assistants, latency SLA <2s
```

---

## Option 2: Delayed Hedge (Start Second Only If First Is Slow)

Wait a short delay before launching the hedge. If the primary responds in time, never send the second request.

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

async def delayed_hedged_request(
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
    hedge_delay_ms: int = 500
) -> dict:
    """
    Start primary request immediately. If it hasn't responded within
    hedge_delay_ms, fire a second request and race them.
    """
    start = time.monotonic()
    hedge_fired = False

    async def primary_request() -> dict:
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages
        )
        return {
            "replica": "primary",
            "text": response.content[0].text,
            "latency_ms": int((time.monotonic() - start) * 1000)
        }

    async def hedge_request() -> dict:
        # Wait before firing hedge
        await asyncio.sleep(hedge_delay_ms / 1000)
        nonlocal hedge_fired
        hedge_fired = True
        response = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            messages=messages
        )
        return {
            "replica": "hedge",
            "text": response.content[0].text,
            "latency_ms": int((time.monotonic() - start) * 1000)
        }

    tasks = [
        asyncio.create_task(primary_request()),
        asyncio.create_task(hedge_request())
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    result = done.pop().result()
    result["hedge_fired"] = hedge_fired
    result["hedge_delay_ms"] = hedge_delay_ms
    return result

async def benchmark_delayed_hedge():
    """Compare standard vs delayed hedged requests."""
    messages = [{"role": "user", "content": "What is the capital of France?"}]

    # Run delayed hedge
    for delay in [300, 500, 800]:
        start = time.monotonic()
        result = await delayed_hedged_request(messages, hedge_delay_ms=delay)
        wall_ms = int((time.monotonic() - start) * 1000)
        print(f"Hedge delay {delay}ms: winner={result['replica']}, "
              f"hedge_fired={result['hedge_fired']}, wall={wall_ms}ms")

asyncio.run(benchmark_delayed_hedge())

# Expected Token Savings: Only fires hedge when primary is slow (~30-40% of requests); reduces token overhead vs dual-request
# Environment: p50 latency acceptable but p95+ too high; want hedge only when needed
```

---

## Option 3: Model-Tier Hedge (Primary Sonnet + Fallback Haiku)

Send a high-quality Sonnet request and a fast Haiku backup. Use Sonnet if it wins, Haiku if it's much faster.

```python
import anthropic
import asyncio
import time

client = anthropic.AsyncAnthropic()

async def tiered_hedged_request(
    messages: list[dict],
    primary_model: str = "claude-sonnet-4-6",
    fallback_model: str = "claude-haiku-4-5-20251001",
    fallback_delay_ms: int = 400,
    max_tokens: int = 512
) -> dict:
    """
    Race a high-quality model against a faster/cheaper model.
    Accept whichever finishes first; the fallback starts after a delay.
    """
    start = time.monotonic()

    async def run_primary() -> dict:
        response = await client.messages.create(
            model=primary_model,
            max_tokens=max_tokens,
            messages=messages
        )
        return {
            "model": primary_model,
            "tier": "primary",
            "text": response.content[0].text,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens
        }

    async def run_fallback() -> dict:
        await asyncio.sleep(fallback_delay_ms / 1000)
        response = await client.messages.create(
            model=fallback_model,
            max_tokens=max_tokens,
            messages=messages
        )
        return {
            "model": fallback_model,
            "tier": "fallback",
            "text": response.content[0].text,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens
        }

    tasks = [
        asyncio.create_task(run_primary()),
        asyncio.create_task(run_fallback())
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    winner = done.pop().result()
    winner["wall_ms"] = int((time.monotonic() - start) * 1000)
    return winner

async def main():
    messages = [{"role": "user", "content": "Summarize the pros and cons of microservices architecture."}]

    result = await tiered_hedged_request(messages)
    print(f"Winner: {result['tier']} ({result['model']})")
    print(f"Latency: {result['latency_ms']}ms | Wall: {result['wall_ms']}ms")
    print(f"Tokens: {result['input_tokens']} in / {result['output_tokens']} out")
    print(f"Response:\n{result['text'][:300]}")

asyncio.run(main())

# Expected Token Savings: If primary wins ~60% of the time, avg quality stays high with Haiku as cheap insurance
# Environment: mixed latency requirements; want best response if fast, acceptable response if primary is slow
```

---

## Option 4: Streaming Hedge with First-Chunk Race

Start streaming from two endpoints; commit to whichever produces its first chunk first, cancel the other.

```python
import anthropic
import asyncio
import time
from contextlib import asynccontextmanager

client = anthropic.AsyncAnthropic()

@asynccontextmanager
async def streaming_hedge(
    messages: list[dict],
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512
):
    """
    Race two streaming requests. Yield the stream from whichever
    produces its first content chunk first.
    """
    start = time.monotonic()
    winner_queue: asyncio.Queue = asyncio.Queue()
    first_chunk_events = [asyncio.Event(), asyncio.Event()]

    collected_text = ["", ""]
    winner_idx = [None]

    async def stream_replica(idx: int):
        async with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            messages=messages
        ) as stream:
            async for text in stream.text_stream:
                collected_text[idx] += text

                # Signal first chunk
                if not first_chunk_events[idx].is_set():
                    first_chunk_events[idx].set()
                    await winner_queue.put((idx, time.monotonic() - start))

                # If we're not the winner, stop consuming
                if winner_idx[0] is not None and winner_idx[0] != idx:
                    return

    tasks = [
        asyncio.create_task(stream_replica(0)),
        asyncio.create_task(stream_replica(1))
    ]

    # Wait for first chunk from either replica
    winning_idx, first_chunk_latency_s = await winner_queue.get()
    winner_idx[0] = winning_idx

    # Cancel loser
    loser_idx = 1 - winning_idx
    tasks[loser_idx].cancel()

    # Wait for winner to finish
    await tasks[winning_idx]

    result = {
        "winner_replica": winning_idx,
        "first_chunk_ms": int(first_chunk_latency_s * 1000),
        "text": collected_text[winning_idx],
        "total_ms": int((time.monotonic() - start) * 1000)
    }

    yield result

    # Cleanup
    for task in tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

async def main():
    messages = [{"role": "user", "content": "Write a haiku about distributed systems."}]

    async with streaming_hedge(messages) as result:
        print(f"First chunk from replica {result['winner_replica']} "
              f"at {result['first_chunk_ms']}ms")
        print(f"Full response ({result['total_ms']}ms):\n{result['text']}")

asyncio.run(main())

# Expected Token Savings: First-chunk commitment at ~200ms; prevents full duplicate completion in majority of races
# Environment: streaming UIs, token-by-token display, real-time chat where first byte matters most
```

---

## Option 5: Budget-Aware Hedge

Only hedge when remaining budget allows it; track hedge cost and disable hedging under budget pressure.

```python
import anthropic
import asyncio
import time
from dataclasses import dataclass, field

client = anthropic.AsyncAnthropic()

# Approximate cost per 1M tokens (USD)
MODEL_COSTS = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
}

@dataclass
class HedgeBudget:
    total_usd: float
    spent_usd: float = 0.0
    requests_hedged: int = 0
    requests_unhedged: int = 0
    hedge_cost_usd: float = 0.0

    @property
    def remaining_usd(self) -> float:
        return self.total_usd - self.spent_usd

    @property
    def hedge_fraction(self) -> float:
        total = self.requests_hedged + self.requests_unhedged
        return self.requests_hedged / total if total > 0 else 0.0

    def estimate_request_cost(self, model: str, input_tokens: int = 500, output_tokens: int = 500) -> float:
        costs = MODEL_COSTS.get(model, {"input": 3.0, "output": 15.0})
        return (input_tokens * costs["input"] + output_tokens * costs["output"]) / 1_000_000

    def should_hedge(self, model: str, hedge_threshold: float = 0.3) -> bool:
        """Hedge only if we have budget headroom >= hedge_threshold of total."""
        hedge_cost = self.estimate_request_cost(model)
        return self.remaining_usd >= (self.total_usd * hedge_threshold + hedge_cost)

    def record_usage(self, model: str, input_tokens: int, output_tokens: int, was_hedged: bool):
        cost = self.estimate_request_cost(model, input_tokens, output_tokens)
        self.spent_usd += cost
        if was_hedged:
            self.requests_hedged += 1
            self.hedge_cost_usd += cost
        else:
            self.requests_unhedged += 1

async def budget_aware_request(
    messages: list[dict],
    budget: HedgeBudget,
    model: str = "claude-sonnet-4-6",
    max_tokens: int = 512,
    hedge_delay_ms: int = 400
) -> dict:
    """Send hedged or un-hedged request based on remaining budget."""
    do_hedge = budget.should_hedge(model)
    start = time.monotonic()

    async def single_request() -> dict:
        resp = await client.messages.create(
            model=model, max_tokens=max_tokens, messages=messages
        )
        return {
            "text": resp.content[0].text,
            "latency_ms": int((time.monotonic() - start) * 1000),
            "input_tokens": resp.usage.input_tokens,
            "output_tokens": resp.usage.output_tokens
        }

    if not do_hedge:
        result = await single_request()
        budget.record_usage(model, result["input_tokens"], result["output_tokens"], False)
        result["hedged"] = False
        return result

    # Hedged path
    async def delayed_replica() -> dict:
        await asyncio.sleep(hedge_delay_ms / 1000)
        return await single_request()

    tasks = [
        asyncio.create_task(single_request()),
        asyncio.create_task(delayed_replica())
    ]

    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    result = done.pop().result()
    budget.record_usage(model, result["input_tokens"], result["output_tokens"], True)
    result["hedged"] = True
    return result

async def main():
    budget = HedgeBudget(total_usd=0.10)  # $0.10 budget
    questions = [
        "What is 2+2?",
        "Explain quantum entanglement.",
        "List 3 Python best practices.",
        "What causes aurora borealis?"
    ]

    for q in questions:
        result = await budget_aware_request(
            [{"role": "user", "content": q}], budget
        )
        print(f"Q: {q[:40]}")
        print(f"  hedged={result['hedged']}, latency={result['latency_ms']}ms, "
              f"budget_left=${budget.remaining_usd:.4f}")

    print(f"\nHedge rate: {budget.hedge_fraction:.0%}")
    print(f"Hedge overhead: ${budget.hedge_cost_usd:.5f}")
    print(f"Total spent: ${budget.spent_usd:.5f}")

asyncio.run(main())

# Expected Token Savings: Disables hedging before budget exhaustion; hedge rate auto-reduces under cost pressure
# Environment: production agents with cost budgets, per-user token limits, metered API usage
```

---

## Option 6: Async Gather with Cancellation Pool

Maintain a pool of N hedged requests with configurable cancel-on-first policy and latency tracking.

```python
import anthropic
import asyncio
import time
import statistics
from dataclasses import dataclass, field
from collections import deque

client = anthropic.AsyncAnthropic()

@dataclass
class HedgeStats:
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=100))
    hedge_wins: int = 0
    primary_wins: int = 0
    total_requests: int = 0

    def record(self, latency_ms: int, was_hedge_winner: bool):
        self.latencies_ms.append(latency_ms)
        self.total_requests += 1
        if was_hedge_winner:
            self.hedge_wins += 1
        else:
            self.primary_wins += 1

    @property
    def p50_ms(self) -> float:
        if not self.latencies_ms:
            return 0
        return statistics.median(self.latencies_ms)

    @property
    def p95_ms(self) -> float:
        if len(self.latencies_ms) < 20:
            return 0
        sorted_l = sorted(self.latencies_ms)
        return sorted_l[int(len(sorted_l) * 0.95)]

    @property
    def hedge_win_rate(self) -> float:
        total = self.hedge_wins + self.primary_wins
        return self.hedge_wins / total if total > 0 else 0

class HedgePool:
    """Manages a pool of hedged LLM requests with latency tracking."""

    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        replicas: int = 2,
        hedge_delay_ms: int = 500,
        max_tokens: int = 512
    ):
        self.model = model
        self.replicas = replicas
        self.hedge_delay_ms = hedge_delay_ms
        self.max_tokens = max_tokens
        self.stats = HedgeStats()

    async def request(self, messages: list[dict]) -> dict:
        """Send N hedged requests; return first, cancel rest."""
        start = time.monotonic()
        result_queue: asyncio.Queue = asyncio.Queue(maxsize=1)

        async def attempt(replica_idx: int):
            # Stagger replicas by hedge_delay_ms * idx
            if replica_idx > 0:
                await asyncio.sleep(self.hedge_delay_ms * replica_idx / 1000)
            try:
                resp = await client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=messages
                )
                latency = int((time.monotonic() - start) * 1000)
                await result_queue.put({
                    "replica": replica_idx,
                    "text": resp.content[0].text,
                    "latency_ms": latency,
                    "input_tokens": resp.usage.input_tokens,
                    "output_tokens": resp.usage.output_tokens
                })
            except asyncio.CancelledError:
                pass

        tasks = [asyncio.create_task(attempt(i)) for i in range(self.replicas)]

        # Wait for first result
        result = await result_queue.get()

        # Cancel remaining tasks
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        is_hedge = result["replica"] > 0
        self.stats.record(result["latency_ms"], is_hedge)
        result["wall_ms"] = int((time.monotonic() - start) * 1000)
        result["replicas_sent"] = self.replicas
        return result

    def get_stats(self) -> dict:
        return {
            "total_requests": self.stats.total_requests,
            "p50_ms": round(self.stats.p50_ms, 1),
            "p95_ms": round(self.stats.p95_ms, 1),
            "hedge_win_rate": round(self.stats.hedge_win_rate, 3),
            "primary_wins": self.stats.primary_wins,
            "hedge_wins": self.stats.hedge_wins
        }

async def main():
    pool = HedgePool(
        model="claude-sonnet-4-6",
        replicas=2,
        hedge_delay_ms=400
    )

    questions = [
        "What is machine learning?",
        "Name three sorting algorithms.",
        "What is TCP/IP?",
        "Explain REST APIs briefly.",
        "What is a closure in programming?"
    ]

    print("Running hedged requests...\n")
    for q in questions:
        result = await pool.request([{"role": "user", "content": q}])
        print(f"Q: {q}")
        print(f"  replica={result['replica']}, latency={result['latency_ms']}ms, wall={result['wall_ms']}ms")
        print(f"  response: {result['text'][:100]}\n")

    stats = pool.get_stats()
    print("=== Pool Statistics ===")
    for k, v in stats.items():
        print(f"  {k}: {v}")

asyncio.run(main())

# Expected Token Savings: 2-replica hedge with 400ms delay fires duplicate ~35% of the time; cost overhead ~35% for 60% p95 improvement
# Environment: high-throughput pipelines, latency dashboards, production API gateways with SLA guarantees
```

---

## Comparison

| Option | Hedge Strategy | Extra Cost | Latency Gain | Best For |
|--------|---------------|------------|-------------|----------|
| 1. Dual-Request Race | Always 2x requests | ~100% | 50-70% p99 | Simple, latency critical |
| 2. Delayed Hedge | 2nd only if slow | ~30-40% avg | 40-60% p99 | Balance cost vs latency |
| 3. Model-Tier Hedge | Sonnet + Haiku fallback | ~15-25% avg | 40-55% p95 | Quality + speed tradeoff |
| 4. Streaming Hedge | First-chunk race | ~25-35% avg | 60-75% time-to-first-token | Streaming UIs |
| 5. Budget-Aware | Hedge only if budget allows | Bounded | Adaptive | Cost-controlled production |
| 6. Async Pool | N-replica configurable | N-1x on slow% | Configurable | High-throughput pipelines |

**Recommended defaults:**
- **User-facing chat** → Option 2 (delayed hedge, 300-500ms threshold)
- **Streaming UI** → Option 4 (first-chunk race)
- **Cost-sensitive prod** → Option 5 (budget-aware)
- **High-throughput API** → Option 6 (pool with stats)
- **Simplest to deploy** → Option 1 (always dual)
