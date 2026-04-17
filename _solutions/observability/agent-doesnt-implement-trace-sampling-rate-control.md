---
title: "Agent Doesn't Implement Trace Sampling Rate Control"
description: "Agents that emit a full distributed trace for every request produce trace volumes that overwhelm storage backends at high throughput — a 1000 RPS agent emitting complete traces generates millions of spans per minute, costing thousands of dollars monthly in trace storage. Implement adaptive trace sampling that applies head-based sampling for normal requests, forces full sampling for errors and slow requests, and adjusts the base sampling rate dynamically based on observed throughput."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-trace-sampling-rate-control
tags: [trace-sampling, distributed-tracing, observability-cost, adaptive-sampling, head-sampling, tail-sampling]
symptoms:
  - "Trace storage costs scale linearly with request volume — no sampling in place"
  - "Every request emits a complete trace regardless of whether it succeeded or failed"
  - "Error traces are sampled at the same rate as success traces — errors can be under-represented"
  - "No way to increase sampling rate dynamically when an incident is being investigated"
  - "Trace backend overwhelmed at peak load — spans dropped at the exporter layer without control"
---

## Why This Happens

Without sampling, every request generates a full trace. At low throughput this is fine; at production scale it is expensive and operationally unmanageable. The core tension is that sampling reduces cost but risks losing the traces you most need — especially error traces and slow outliers. Adaptive sampling resolves this with a two-tier approach: a low base rate (1–5%) for normal successful requests, and forced 100% sampling for requests that are errors, exceed a latency threshold, or match a specific trace ID prefix for targeted debugging. This preserves full coverage of important signals while dramatically reducing volume from routine requests.

## Solution 1: Sampling Policy

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class SamplingDecision(str, Enum):
    SAMPLE = "sample"
    DROP = "drop"
    FORCE_SAMPLE = "force_sample"   # always sampled, even if base rate would drop


@dataclass
class TraceSamplingPolicy:
    base_rate: float = 0.05              # 5% of normal requests
    error_sample_rate: float = 1.0       # 100% of errors
    slow_request_threshold_ms: float = 2000.0  # force-sample above this latency
    slow_request_sample_rate: float = 1.0
    min_rate: float = 0.001              # floor when auto-adjusting down
    max_rate: float = 1.0               # ceiling
    force_sample_trace_id_prefixes: List[str] = field(default_factory=list)
    # trace IDs starting with these prefixes are always sampled (for debugging)
```

## Solution 2: Sampling Decision Engine

```python
import hashlib
import random
from typing import Optional


class TraceSamplingDecisionEngine:
    """
    Makes per-trace sampling decisions based on the policy.
    Uses trace ID hashing for deterministic head-based sampling
    so that all spans in a trace are consistently included or excluded.
    """

    def __init__(self, policy: TraceSamplingPolicy):
        self._policy = policy

    def decide(
        self,
        trace_id: str,
        is_error: bool = False,
        latency_ms: Optional[float] = None,
    ) -> SamplingDecision:
        # Force-sample for targeted debugging
        for prefix in self._policy.force_sample_trace_id_prefixes:
            if trace_id.startswith(prefix):
                return SamplingDecision.FORCE_SAMPLE

        # Force-sample errors
        if is_error and self._policy.error_sample_rate >= 1.0:
            return SamplingDecision.FORCE_SAMPLE
        if is_error and random.random() < self._policy.error_sample_rate:
            return SamplingDecision.FORCE_SAMPLE

        # Force-sample slow requests
        if (
            latency_ms is not None
            and latency_ms >= self._policy.slow_request_threshold_ms
            and random.random() < self._policy.slow_request_sample_rate
        ):
            return SamplingDecision.FORCE_SAMPLE

        # Base rate: deterministic per trace_id for consistency
        hash_val = int(hashlib.sha256(trace_id.encode()).hexdigest()[:8], 16)
        normalized = hash_val / (2 ** 32)
        if normalized < self._policy.base_rate:
            return SamplingDecision.SAMPLE

        return SamplingDecision.DROP
```

## Solution 3: Adaptive Rate Controller

```python
import time
from threading import Lock
from typing import Deque
from collections import deque


class AdaptiveSamplingRateController:
    """
    Adjusts the base sampling rate dynamically based on observed
    request throughput. At high throughput, rate decreases to control
    volume; at low throughput, rate increases to maintain coverage.
    """

    def __init__(
        self,
        policy: TraceSamplingPolicy,
        target_sampled_rps: float = 10.0,    # desired sampled traces per second
        adjustment_window_seconds: float = 60.0,
        adjustment_interval_seconds: float = 15.0,
    ):
        self._policy = policy
        self._target_sampled_rps = target_sampled_rps
        self._window = adjustment_window_seconds
        self._interval = adjustment_interval_seconds
        self._request_times: Deque[float] = deque()
        self._last_adjusted = time.time()
        self._lock = Lock()

    def record_request(self) -> None:
        now = time.time()
        with self._lock:
            self._request_times.append(now)
            cutoff = now - self._window
            while self._request_times and self._request_times[0] < cutoff:
                self._request_times.popleft()

    def maybe_adjust(self) -> Optional[float]:
        """Returns the new rate if adjusted, else None."""
        now = time.time()
        with self._lock:
            if now - self._last_adjusted < self._interval:
                return None
            self._last_adjusted = now
            count = len(self._request_times)

        observed_rps = count / self._window if count > 0 else 0.0
        if observed_rps <= 0:
            return None

        # Target: sampled_rps / observed_rps
        desired_rate = self._target_sampled_rps / observed_rps
        new_rate = max(self._policy.min_rate, min(self._policy.max_rate, desired_rate))

        with self._lock:
            old_rate = self._policy.base_rate
            self._policy.base_rate = round(new_rate, 6)

        return new_rate if abs(new_rate - old_rate) > 0.001 else None
```

## Solution 4: Sampling-Aware Trace Emitter

```python
import time
from typing import Any, Callable, Dict, List, Optional


class SamplingAwareTraceEmitter:
    """
    Wraps span emission with sampling decision application.
    Spans from dropped traces are discarded; force-sampled and
    sampled traces are forwarded to the backend emitter.
    """

    def __init__(
        self,
        engine: TraceSamplingDecisionEngine,
        controller: AdaptiveSamplingRateController,
        emit_fn: Callable[[dict], None],
    ):
        self._engine = engine
        self._controller = controller
        self._emit = emit_fn
        self._emitted = 0
        self._dropped = 0
        self._force_sampled = 0

    def emit_span(
        self,
        trace_id: str,
        span: dict,
        is_error: bool = False,
        latency_ms: Optional[float] = None,
    ) -> SamplingDecision:
        self._controller.record_request()
        self._controller.maybe_adjust()

        decision = self._engine.decide(
            trace_id=trace_id,
            is_error=is_error,
            latency_ms=latency_ms,
        )

        if decision == SamplingDecision.DROP:
            self._dropped += 1
        else:
            span["sampling_decision"] = decision.value
            self._emit(span)
            self._emitted += 1
            if decision == SamplingDecision.FORCE_SAMPLE:
                self._force_sampled += 1

        return decision

    def stats(self) -> dict:
        total = self._emitted + self._dropped
        return {
            "total_spans": total,
            "emitted": self._emitted,
            "dropped": self._dropped,
            "force_sampled": self._force_sampled,
            "effective_sample_rate": round(self._emitted / max(total, 1), 4),
        }
```

## Solution 5: Sampling Override Manager

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class TraceSamplingOverrideManager:
    """
    Allows operators to temporarily force 100% sampling for specific
    trace ID prefixes (for incident investigation) with automatic expiry.
    """

    def __init__(self, policy: TraceSamplingPolicy):
        self._policy = policy
        self._overrides: Dict[str, float] = {}  # prefix -> expiry timestamp
        self._lock = Lock()

    def add_prefix_override(
        self,
        prefix: str,
        duration_seconds: float = 300.0,
    ) -> None:
        expiry = time.time() + duration_seconds
        with self._lock:
            self._overrides[prefix] = expiry
            self._policy.force_sample_trace_id_prefixes = list(
                set(self._policy.force_sample_trace_id_prefixes) | {prefix}
            )

    def remove_prefix_override(self, prefix: str) -> None:
        with self._lock:
            self._overrides.pop(prefix, None)
            if prefix in self._policy.force_sample_trace_id_prefixes:
                self._policy.force_sample_trace_id_prefixes.remove(prefix)

    def evict_expired(self) -> List[str]:
        now = time.time()
        with self._lock:
            expired = [p for p, exp in self._overrides.items() if exp < now]
            for p in expired:
                del self._overrides[p]
                if p in self._policy.force_sample_trace_id_prefixes:
                    self._policy.force_sample_trace_id_prefixes.remove(p)
        return expired

    def active_overrides(self) -> List[dict]:
        now = time.time()
        with self._lock:
            return [
                {"prefix": p, "expires_in_seconds": round(exp - now, 1)}
                for p, exp in self._overrides.items()
                if exp > now
            ]
```

## Solution 6: Trace Sampling Dashboard

```python
import time


class TraceSamplingDashboard:
    """
    Combines emitter stats, current policy, and active overrides
    into a single operational view.
    """

    def __init__(
        self,
        emitter: SamplingAwareTraceEmitter,
        policy: TraceSamplingPolicy,
        override_manager: TraceSamplingOverrideManager,
        controller: AdaptiveSamplingRateController,
    ):
        self._emitter = emitter
        self._policy = policy
        self._overrides = override_manager
        self._controller = controller

    def render(self) -> dict:
        self._overrides.evict_expired()
        return {
            "generated_at": time.time(),
            "current_policy": {
                "base_rate": self._policy.base_rate,
                "error_sample_rate": self._policy.error_sample_rate,
                "slow_request_threshold_ms": self._policy.slow_request_threshold_ms,
            },
            "emitter_stats": self._emitter.stats(),
            "active_overrides": self._overrides.active_overrides(),
        }
```

## Comparison

| Approach | Head Sampling | Error Force-Sample | Adaptive Rate | Override Management | Dashboard |
|---|---|---|---|---|---|
| TraceSamplingDecisionEngine | Yes (hash-based) | Yes | No | Via policy | No |
| AdaptiveSamplingRateController | No | No | Yes (RPS-based) | No | No |
| SamplingAwareTraceEmitter | Via engine | Via engine | Via controller | No | Stats |
| TraceSamplingOverrideManager | No | No | No | Yes (TTL-based) | No |
| TraceSamplingDashboard | No | No | No | No | Yes |

**Best for production**: Start with `base_rate=0.05` (5%) and `target_sampled_rps=10` — at 200 RPS steady state this produces 10 sampled traces/second, which is sufficient for statistical analysis without overwhelming storage. Always keep `error_sample_rate=1.0` — error traces are your most valuable debugging signal and should never be sampled away. Use `TraceSamplingOverrideManager.add_prefix_override()` when investigating a specific user session or request type; set `duration_seconds=300` so overrides automatically expire after the investigation window. Monitor `effective_sample_rate` in the dashboard: if it drifts significantly above `base_rate`, force-sampling is dominating (many errors or slow requests), which is itself a signal worth investigating.
