---
layout: solution
title: "Agent Doesn't Implement Streaming Token Usage Tracking"
category: streaming
description: "When streaming responses, the agent discards the final usage data emitted at stream end, making it impossible to track token costs, set spend alerts, or enforce per-request budgets."
tags: [streaming, token-cost, observability, monitoring, production]
---

## Symptom

The agent streams responses correctly but token usage (`input_tokens`, `output_tokens`) is never recorded. Cost monitoring dashboards show zero. Per-user token budgets cannot be enforced because spend is invisible. After switching from batch to streaming mode, the team loses all cost visibility and discovers the billing surprise at month-end.

## Root Cause

The `client.messages.create()` response always includes `response.usage`. In streaming mode, token counts are available at stream end via `stream.get_final_message().usage`, but this requires explicitly calling `get_final_message()` after the stream closes — something that is easily forgotten when the focus is on processing the delta stream. Developers who only iterate over `stream.text_stream` never see the usage data.

## Fix

### Option 1 — Capture usage from get_final_message() after stream

```python
import anthropic
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class StreamUsage:
    input_tokens:  int
    output_tokens: int
    stop_reason:   str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def estimated_cost_usd(self, model: str = "claude-haiku-4-5-20251001") -> float:
        rates = {
            "claude-haiku-4-5-20251001": (0.25, 1.25),
            "claude-sonnet-4-6":          (3.00, 15.00),
            "claude-opus-4-6":            (15.00, 75.00),
        }
        in_rate, out_rate = rates.get(model, (3.0, 15.0))
        return (self.input_tokens * in_rate + self.output_tokens * out_rate) / 1_000_000

def stream_with_usage(prompt: str, model: str = "claude-haiku-4-5-20251001") -> tuple[str, StreamUsage]:
    text = ""
    with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for delta in stream.text_stream:
            text += delta
            print(delta, end="", flush=True)
        # CRITICAL: get_final_message() must be called before the context manager exits
        final   = stream.get_final_message()
        usage   = StreamUsage(
            input_tokens  = final.usage.input_tokens,
            output_tokens = final.usage.output_tokens,
            stop_reason   = final.stop_reason,
        )
    print()
    return text, usage

text, usage = stream_with_usage("Explain how HTTPS works.")
print(f"\n[usage] in={usage.input_tokens} out={usage.output_tokens} "
      f"total={usage.total_tokens} cost=${usage.estimated_cost_usd():.5f}")
```

**Expected Token Savings:** Usage tracking itself doesn't save tokens, but accurate cost data enables targeted optimisation that typically reduces spend by 20–40% once operators can see where tokens are going.
**Environment:** Any streaming agent in production; critical for cost monitoring and per-user billing.

---

### Option 2 — Async streaming with per-request usage ledger

```python
import asyncio
import anthropic
from collections import defaultdict
from dataclasses import dataclass, field
from threading import Lock

client = anthropic.AsyncAnthropic()

COST_PER_M = {
    "claude-haiku-4-5-20251001": (0.25, 1.25),
    "claude-sonnet-4-6":          (3.00, 15.00),
}

@dataclass
class UsageLedger:
    _records: list[dict] = field(default_factory=list)
    _lock:    Lock        = field(default_factory=Lock)

    def record(self, user_id: str, model: str, input_tokens: int, output_tokens: int) -> None:
        in_r, out_r = COST_PER_M.get(model, (3.0, 15.0))
        cost = (input_tokens * in_r + output_tokens * out_r) / 1_000_000
        with self._lock:
            self._records.append({
                "user_id": user_id, "model": model,
                "input": input_tokens, "output": output_tokens, "cost_usd": cost,
            })

    def total_cost(self, user_id: str | None = None) -> float:
        with self._lock:
            records = self._records if user_id is None else [r for r in self._records if r["user_id"] == user_id]
        return sum(r["cost_usd"] for r in records)

    def report(self) -> None:
        with self._lock:
            by_user: dict[str, float] = defaultdict(float)
            for r in self._records:
                by_user[r["user_id"]] += r["cost_usd"]
        print("\n=== Usage Report ===")
        for uid, cost in sorted(by_user.items()):
            print(f"  {uid}: ${cost:.5f}")
        print(f"  TOTAL: ${self.total_cost():.5f}")

ledger = UsageLedger()

async def stream_for_user(user_id: str, prompt: str) -> str:
    model = "claude-haiku-4-5-20251001"
    text  = ""
    async with client.messages.stream(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for delta in stream.text_stream:
            text += delta
        final = await stream.get_final_message()
        ledger.record(user_id, model, final.usage.input_tokens, final.usage.output_tokens)
    return text

async def main():
    tasks = [
        stream_for_user("alice", "What is gradient descent?"),
        stream_for_user("bob",   "Explain REST APIs."),
        stream_for_user("alice", "What is backpropagation?"),
        stream_for_user("carol", "Define machine learning."),
    ]
    await asyncio.gather(*tasks)
    ledger.report()

asyncio.run(main())
```

**Expected Token Savings:** Per-user ledger enables chargeback and per-user spend limits; users exceeding their budget can be blocked before the next request rather than after month-end billing.
**Environment:** Multi-tenant SaaS agents; platforms that need per-user token billing or quotas.

---

### Option 3 — Running estimate during stream using character count heuristic

```python
import anthropic
import time

client = anthropic.Anthropic()

class LiveTokenEstimator:
    """
    Estimates token usage in real-time during streaming.
    Corrects to exact count when the stream ends.
    """
    CHARS_PER_TOKEN = 4.0

    def __init__(self, model: str = "claude-haiku-4-5-20251001"):
        self.model         = model
        self._input_exact  = 0
        self._output_chars = 0
        self._output_exact = 0
        self._start        = time.monotonic()

    def set_input_tokens(self, n: int) -> None:
        self._input_exact = n

    def on_delta(self, delta: str) -> None:
        self._output_chars += len(delta)

    def finalize(self, exact_output: int) -> None:
        self._output_exact = exact_output

    @property
    def estimated_output_tokens(self) -> int:
        return max(1, int(self._output_chars / self.CHARS_PER_TOKEN))

    @property
    def exact_output_tokens(self) -> int:
        return self._output_exact or self.estimated_output_tokens

    def cost_usd(self) -> float:
        rates = {
            "claude-haiku-4-5-20251001": (0.25, 1.25),
            "claude-sonnet-4-6":          (3.00, 15.00),
        }
        in_r, out_r = rates.get(self.model, (3.0, 15.0))
        return (self._input_exact * in_r + self.exact_output_tokens * out_r) / 1_000_000

def stream_with_live_estimate(prompt: str) -> str:
    model     = "claude-haiku-4-5-20251001"
    estimator = LiveTokenEstimator(model)
    text      = ""

    with client.messages.stream(
        model=model,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for delta in stream.text_stream:
            text += delta
            estimator.on_delta(delta)
            print(delta, end="", flush=True)

        final = stream.get_final_message()
        estimator.set_input_tokens(final.usage.input_tokens)
        estimator.finalize(final.usage.output_tokens)

    print()
    print(f"[usage] input={estimator._input_exact} output={estimator.exact_output_tokens} "
          f"cost=${estimator.cost_usd():.5f} time={time.monotonic()-estimator._start:.2f}s")
    return text

stream_with_live_estimate("Write a detailed explanation of how blockchain works.")
```

**Expected Token Savings:** Live estimates let you abort a stream that is growing too expensive before it completes; mid-stream budget enforcement requires a running count, not just the final total.
**Environment:** Agents with per-request token budgets; streaming pipelines where cost overruns must be caught before completion.

---

### Option 4 — Middleware wrapper that always captures usage from any stream

```python
import anthropic
from contextlib import contextmanager
from typing import Iterator, Callable

client = anthropic.Anthropic()

_usage_callbacks: list[Callable[[dict], None]] = []

def register_usage_callback(fn: Callable[[dict], None]) -> None:
    """Register a function to receive usage data after every streamed response."""
    _usage_callbacks.append(fn)

def _fire_usage(model: str, input_tokens: int, output_tokens: int, stop_reason: str) -> None:
    data = {
        "model": model,
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "total_tokens":  input_tokens + output_tokens,
        "stop_reason":   stop_reason,
    }
    for cb in _usage_callbacks:
        try:
            cb(data)
        except Exception as exc:
            print(f"[usage_callback] error: {exc}")

@contextmanager
def tracked_stream(model: str, max_tokens: int, messages: list[dict]) -> Iterator[str]:
    """Drop-in replacement for client.messages.stream that always fires usage callbacks."""
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
    ) as stream:
        yield stream.text_stream
        final = stream.get_final_message()
        _fire_usage(
            model,
            final.usage.input_tokens,
            final.usage.output_tokens,
            final.stop_reason,
        )

# ── usage handlers ─────────────────────────────────────────────────────────────

total_cost   = [0.0]
total_tokens = [0]

def log_to_console(usage: dict) -> None:
    rates = {"claude-haiku-4-5-20251001": (0.25, 1.25), "claude-sonnet-4-6": (3.0, 15.0)}
    in_r, out_r = rates.get(usage["model"], (3.0, 15.0))
    cost = (usage["input_tokens"] * in_r + usage["output_tokens"] * out_r) / 1_000_000
    total_cost[0]   += cost
    total_tokens[0] += usage["total_tokens"]
    print(f"[usage] in={usage['input_tokens']} out={usage['output_tokens']} cost=${cost:.5f}")

register_usage_callback(log_to_console)

# ── usage ──────────────────────────────────────────────────────────────────────

for prompt in ["Explain REST.", "What is TCP?", "Define DNS."]:
    text = ""
    with tracked_stream("claude-haiku-4-5-20251001", 128, [{"role": "user", "content": prompt}]) as stream:
        for delta in stream:
            text += delta
    print(f"Response: {text[:60]}\n")

print(f"Session: {total_tokens[0]} tokens, ${total_cost[0]:.5f}")
```

**Expected Token Savings:** Middleware guarantees usage is captured regardless of which team member wrote the calling code; no individual call site can accidentally omit usage tracking.
**Environment:** Teams with multiple developers working on the same agent codebase; shared platform code where usage tracking must be enforced uniformly.

---

### Option 5 — Per-model cost tracker with budget enforcement

```python
import anthropic
import threading
from dataclasses import dataclass, field

client = anthropic.Anthropic()

COSTS_PER_M = {
    "claude-haiku-4-5-20251001": (0.25,  1.25),
    "claude-sonnet-4-6":          (3.00,  15.00),
    "claude-opus-4-6":            (15.00, 75.00),
}

@dataclass
class BudgetTracker:
    budget_usd: float
    _spent:     float = field(default=0.0, init=False)
    _lock:      threading.Lock = field(default_factory=threading.Lock, init=False)
    _calls:     int = field(default=0, init=False)

    def record(self, model: str, input_tokens: int, output_tokens: int) -> None:
        in_r, out_r = COSTS_PER_M.get(model, (3.0, 15.0))
        cost = (input_tokens * in_r + output_tokens * out_r) / 1_000_000
        with self._lock:
            self._spent += cost
            self._calls += 1

    def remaining(self) -> float:
        with self._lock:
            return self.budget_usd - self._spent

    def is_exhausted(self) -> bool:
        return self.remaining() <= 0

    def report(self) -> None:
        with self._lock:
            print(f"\n[budget] ${self._spent:.5f} / ${self.budget_usd:.2f} "
                  f"({self._spent/self.budget_usd:.0%}) across {self._calls} calls")

tracker = BudgetTracker(budget_usd=0.10)  # 10 cent session budget

class BudgetExceeded(RuntimeError):
    pass

def stream_within_budget(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    if tracker.is_exhausted():
        raise BudgetExceeded(f"Budget exhausted (spent ${tracker._spent:.5f})")

    text = ""
    with client.messages.stream(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for delta in stream.text_stream:
            text += delta
        final = stream.get_final_message()
        tracker.record(model, final.usage.input_tokens, final.usage.output_tokens)
        print(f"[budget] remaining: ${tracker.remaining():.5f}")
    return text

prompts = [f"What is programming concept #{i}?" for i in range(20)]
for p in prompts:
    try:
        result = stream_within_budget(p)
        print(f"  {result[:60]}")
    except BudgetExceeded as e:
        print(f"[stop] {e}")
        break

tracker.report()
```

**Expected Token Savings:** Hard budget enforcement prevents runaway loops from exceeding a per-session cost ceiling; token usage tracking is the prerequisite for any spend control.
**Environment:** Batch processing agents with fixed cost ceilings; demo or trial accounts where token spend must be capped per session.

---

### Option 6 — Prometheus counter integration for streaming token metrics

```python
import anthropic
import time

# pip install prometheus-client
try:
    from prometheus_client import Counter, Histogram, start_http_server
    PROMETHEUS = True
    start_http_server(8001)

    TOKEN_COUNTER = Counter("agent_tokens_total", "Tokens consumed", ["model", "direction"])
    COST_COUNTER  = Counter("agent_cost_usd_total", "Estimated cost in USD", ["model"])
    LATENCY_HIST  = Histogram("agent_stream_latency_seconds", "Stream latency",
                              buckets=[0.5, 1, 2, 5, 10, 30])
    print("[metrics] Prometheus on :8001/metrics")
except ImportError:
    PROMETHEUS = False
    print("[metrics] prometheus_client not installed — logging only")

client = anthropic.Anthropic()

COSTS = {"claude-haiku-4-5-20251001": (0.25, 1.25), "claude-sonnet-4-6": (3.0, 15.0)}

def stream_with_prometheus(prompt: str, model: str = "claude-haiku-4-5-20251001") -> str:
    text  = ""
    start = time.monotonic()

    with client.messages.stream(
        model=model,
        max_tokens=256,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for delta in stream.text_stream:
            text += delta

        final   = stream.get_final_message()
        elapsed = time.monotonic() - start

        in_tok  = final.usage.input_tokens
        out_tok = final.usage.output_tokens
        in_r, out_r = COSTS.get(model, (3.0, 15.0))
        cost    = (in_tok * in_r + out_tok * out_r) / 1_000_000

        if PROMETHEUS:
            TOKEN_COUNTER.labels(model=model, direction="input").inc(in_tok)
            TOKEN_COUNTER.labels(model=model, direction="output").inc(out_tok)
            COST_COUNTER.labels(model=model).inc(cost)
            LATENCY_HIST.observe(elapsed)

        print(f"[metrics] in={in_tok} out={out_tok} cost=${cost:.5f} latency={elapsed:.2f}s")

    return text

for prompt in ["Explain DNS.", "What is a load balancer?", "Define API gateway."]:
    print(stream_with_prometheus(prompt)[:60])
    print()

time.sleep(2)  # allow Prometheus scrape
```

**Expected Token Savings:** Prometheus metrics expose token cost trends over time; latency histograms show when slow responses are burning disproportionate output tokens.
**Environment:** Kubernetes-hosted agents with Prometheus/Grafana; any production system with existing metrics infrastructure.

---

## Comparison

| Option | Usage Source | Async Safe | Budget Enforcement | External Backend | Best For |
|---|---|---|---|---|---|
| 1. get_final_message() | Exact API count | No | No | No | Baseline: any streaming agent |
| 2. Per-user ledger | Exact API count | Yes | Via ledger | No | Multi-tenant; per-user billing |
| 3. Live estimator | Estimate + exact | No | Partial | No | Mid-stream budget checks |
| 4. Middleware wrapper | Exact API count | No | No | Via callbacks | Shared codebases; enforcement |
| 5. Budget tracker | Exact API count | Thread-safe | Yes | No | Fixed-budget sessions; batch jobs |
| 6. Prometheus counters | Exact API count | No | No | Yes | Production monitoring; Grafana |
