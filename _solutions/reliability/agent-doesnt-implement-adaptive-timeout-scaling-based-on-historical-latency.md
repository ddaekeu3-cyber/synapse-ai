---
title: "Agent Doesn't Implement Adaptive Timeout Scaling Based on Historical Latency"
description: "Agents that use fixed timeouts for tool calls either time out too aggressively on occasionally slow tools, causing unnecessary failures, or wait too long on consistently fast tools, delaying error detection. Implement adaptive timeout scaling that adjusts per-tool timeouts based on historical latency percentiles, expanding during degraded periods and contracting when service recovers."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-adaptive-timeout-scaling-based-on-historical-latency
tags: [adaptive-timeout, latency-percentiles, timeout-scaling, tool-reliability, p99-latency, dynamic-configuration]
symptoms:
  - "Fixed timeouts cause spurious failures when a normally fast tool has a slow spike"
  - "Timeouts set conservatively high waste seconds waiting on tools that are actually down"
  - "No per-tool timeout differentiation — all tools share the same global timeout"
  - "After a service degrades and recovers, timeouts remain at the degraded value forever"
  - "On-call engineers manually tune timeouts after every incident"
---

## Why This Happens

A fixed timeout is a guess made at deployment time about future latency. As services change — deployments, load increases, infrastructure migrations — the appropriate timeout changes too. Adaptive timeout scaling continuously samples actual latency, computes a target percentile (e.g., P99), applies a safety multiplier, and uses that as the live timeout. When a service degrades, the timeout expands to accommodate; when it recovers, the timeout contracts so failures are detected quickly again. Without this feedback loop, timeouts are always either too tight or too loose.

## Solution 1: Latency Sample Store

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class LatencySampleStore:
    """
    Stores recent latency observations for a single tool.
    Supports percentile computation over a configurable time window.
    """

    def __init__(self, window_seconds: int = 600, max_samples: int = 10000):
        self._window = window_seconds
        self._max = max_samples
        self._samples: Deque[Tuple[float, float]] = deque()  # (ts, latency_ms)
        self._lock = Lock()

    def record(self, latency_ms: float) -> None:
        now = time.time()
        with self._lock:
            self._samples.append((now, latency_ms))
            if len(self._samples) > self._max:
                self._samples.popleft()

    def percentile(self, pct: float, sub_window_seconds: Optional[int] = None) -> Optional[float]:
        now = time.time()
        cutoff = now - (sub_window_seconds or self._window)
        with self._lock:
            values = sorted(ms for ts, ms in self._samples if ts >= cutoff)
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def sample_count(self, sub_window_seconds: Optional[int] = None) -> int:
        now = time.time()
        cutoff = now - (sub_window_seconds or self._window)
        with self._lock:
            return sum(1 for ts, _ in self._samples if ts >= cutoff)
```

## Solution 2: Adaptive Timeout Calculator

```python
from dataclasses import dataclass
from typing import Optional


@dataclass
class AdaptiveTimeoutConfig:
    target_percentile: float = 99.0      # use P99 latency as base
    safety_multiplier: float = 2.0       # timeout = P99 * multiplier
    min_timeout_ms: float = 500.0        # never go below this
    max_timeout_ms: float = 30000.0      # never exceed this
    fallback_timeout_ms: float = 5000.0  # used when no samples available
    min_samples_required: int = 20       # samples needed before adapting
    smoothing_alpha: float = 0.2         # EMA smoothing for timeout updates


class AdaptiveTimeoutCalculator:
    """
    Computes a timeout value for a tool based on its latency history.
    Applies EMA smoothing to prevent timeout thrashing during transient spikes.
    """

    def __init__(self, config: AdaptiveTimeoutConfig):
        self._config = config
        self._smoothed: dict = {}   # tool_name -> smoothed timeout ms

    def compute(self, tool_name: str, store: LatencySampleStore) -> float:
        cfg = self._config
        count = store.sample_count()

        if count < cfg.min_samples_required:
            return self._smoothed.get(tool_name, cfg.fallback_timeout_ms)

        p_value = store.percentile(cfg.target_percentile)
        if p_value is None:
            return self._smoothed.get(tool_name, cfg.fallback_timeout_ms)

        raw_timeout = p_value * cfg.safety_multiplier
        clamped = max(cfg.min_timeout_ms, min(cfg.max_timeout_ms, raw_timeout))

        prev = self._smoothed.get(tool_name, clamped)
        smoothed = prev + cfg.smoothing_alpha * (clamped - prev)
        self._smoothed[tool_name] = smoothed
        return round(smoothed, 1)

    def current_timeout(self, tool_name: str) -> Optional[float]:
        return self._smoothed.get(tool_name)
```

## Solution 3: Adaptive Timeout Registry

```python
from typing import Dict, Optional


class AdaptiveTimeoutRegistry:
    """
    Manages per-tool latency stores and timeout calculators.
    Provides a single interface to record latency and retrieve current timeout.
    """

    def __init__(
        self,
        calculator: AdaptiveTimeoutCalculator,
        window_seconds: int = 600,
    ):
        self._calculator = calculator
        self._window = window_seconds
        self._stores: Dict[str, LatencySampleStore] = {}

    def _get_store(self, tool_name: str) -> LatencySampleStore:
        if tool_name not in self._stores:
            self._stores[tool_name] = LatencySampleStore(self._window)
        return self._stores[tool_name]

    def record(self, tool_name: str, latency_ms: float) -> None:
        self._get_store(tool_name).record(latency_ms)

    def timeout_ms(self, tool_name: str) -> float:
        store = self._get_store(tool_name)
        return self._calculator.compute(tool_name, store)

    def timeout_seconds(self, tool_name: str) -> float:
        return self.timeout_ms(tool_name) / 1000.0

    def all_timeouts(self) -> dict:
        return {
            name: self._calculator.compute(name, store)
            for name, store in self._stores.items()
        }
```

## Solution 4: Adaptive Timeout Tool Executor

```python
import asyncio
import time
from typing import Any, Callable


class AdaptiveTimeoutToolExecutor:
    """
    Executes tool calls with dynamically computed timeouts.
    Records actual latency after each call to feed the adaptation loop.
    """

    def __init__(self, registry: AdaptiveTimeoutRegistry):
        self._registry = registry
        self._timeout_hits = 0
        self._total_calls = 0

    async def execute(
        self,
        tool_name: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        timeout_s = self._registry.timeout_seconds(tool_name)
        start = time.time()
        self._total_calls += 1
        timed_out = False

        try:
            result = await asyncio.wait_for(fn(*args, **kwargs), timeout=timeout_s)
            latency_ms = (time.time() - start) * 1000
            self._registry.record(tool_name, latency_ms)
            return {
                "result": result,
                "latency_ms": round(latency_ms, 2),
                "timeout_used_ms": round(timeout_s * 1000, 1),
                "timed_out": False,
            }
        except asyncio.TimeoutError:
            latency_ms = (time.time() - start) * 1000
            self._registry.record(tool_name, latency_ms)
            self._timeout_hits += 1
            timed_out = True
            raise ToolTimeoutError(tool_name, timeout_s)
        except Exception:
            latency_ms = (time.time() - start) * 1000
            self._registry.record(tool_name, latency_ms)
            raise

    def timeout_hit_rate(self) -> float:
        return self._timeout_hits / max(self._total_calls, 1)

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "timeout_hits": self._timeout_hits,
            "timeout_hit_rate": round(self.timeout_hit_rate(), 4),
        }


class ToolTimeoutError(Exception):
    def __init__(self, tool_name: str, timeout_s: float):
        super().__init__(f"tool '{tool_name}' timed out after {timeout_s:.2f}s")
        self.tool_name = tool_name
        self.timeout_s = timeout_s
```

## Solution 5: Timeout Drift Detector

```python
import time
from typing import Dict, List


class TimeoutDriftDetector:
    """
    Detects when a tool's adaptive timeout has drifted significantly from
    its baseline — indicating a persistent degradation that may need escalation.
    """

    def __init__(
        self,
        registry: AdaptiveTimeoutRegistry,
        drift_threshold_pct: float = 100.0,  # alert if timeout doubles
    ):
        self._registry = registry
        self._threshold = drift_threshold_pct / 100.0
        self._baselines: Dict[str, float] = {}
        self._baseline_at: Dict[str, float] = {}

    def establish_baseline(self, tool_name: str) -> None:
        timeout = self._registry.timeout_ms(tool_name)
        self._baselines[tool_name] = timeout
        self._baseline_at[tool_name] = time.time()

    def check_drift(self, tool_name: str) -> dict:
        current = self._registry.timeout_ms(tool_name)
        baseline = self._baselines.get(tool_name)

        if baseline is None:
            return {"tool_name": tool_name, "status": "no_baseline"}

        drift = (current - baseline) / max(baseline, 1)
        drifted = drift > self._threshold

        return {
            "tool_name": tool_name,
            "baseline_ms": round(baseline, 1),
            "current_ms": round(current, 1),
            "drift_pct": round(drift * 100, 1),
            "drifted": drifted,
            "baseline_age_seconds": round(time.time() - self._baseline_at.get(tool_name, time.time()), 1),
        }

    def drifted_tools(self) -> List[dict]:
        results = []
        for tool_name in self._baselines:
            result = self.check_drift(tool_name)
            if result.get("drifted"):
                results.append(result)
        return results
```

## Solution 6: Adaptive Timeout Dashboard

```python
import time


class AdaptiveTimeoutDashboard:
    """
    Combines current timeouts, drift analysis, and executor statistics
    into a single operational snapshot.
    """

    def __init__(
        self,
        registry: AdaptiveTimeoutRegistry,
        executor: AdaptiveTimeoutToolExecutor,
        drift_detector: TimeoutDriftDetector,
    ):
        self._registry = registry
        self._executor = executor
        self._drift = drift_detector

    def render(self) -> dict:
        all_timeouts = self._registry.all_timeouts()
        drifted = self._drift.drifted_tools()

        return {
            "generated_at": time.time(),
            "executor_stats": self._executor.stats(),
            "current_timeouts_ms": {k: round(v, 1) for k, v in all_timeouts.items()},
            "drifted_tools": drifted,
            "drift_alert": len(drifted) > 0,
        }
```

## Comparison

| Approach | Latency Sampling | Percentile Compute | EMA Smoothing | Drift Detection | Dashboard |
|---|---|---|---|---|---|
| LatencySampleStore | Yes (sliding window) | Yes | No | No | No |
| AdaptiveTimeoutCalculator | Via store | Via store | Yes | No | No |
| AdaptiveTimeoutRegistry | Via store | Via calculator | Via calculator | No | No |
| AdaptiveTimeoutToolExecutor | Records latency | No | No | No | No |
| TimeoutDriftDetector | No | No | No | Yes | No |
| AdaptiveTimeoutDashboard | No | No | No | Via detector | Yes |

**Best for production**: Use `target_percentile=99.0` with `safety_multiplier=2.0` — this gives a timeout that accommodates the worst 1% of requests with a 2× buffer before declaring failure. Set `window_seconds=600` (10 minutes) so the adaptive timeout responds to degradations within minutes rather than hours. Run `TimeoutDriftDetector.establish_baseline()` after each deployment so drift is measured relative to post-deployment behavior, not pre-deployment behavior. Alert when `timeout_hit_rate` exceeds 1% — at that point the adaptive timeout is not expanding fast enough and the service may need circuit-breaking instead.
