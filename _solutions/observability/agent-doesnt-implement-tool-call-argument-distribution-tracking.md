---
title: "Agent Doesn't Implement Tool Call Argument Distribution Tracking"
description: "Agents that log tool call counts without tracking argument distributions cannot detect when the LLM starts generating systematically different arguments after a prompt change: the search tool is called the same number of times but always with the same two-word query instead of diverse queries, or a date argument drifts to always use today's date instead of the contextually appropriate date. Implement tool call argument distribution tracking that monitors argument value patterns and alerts on distribution shifts."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-call-argument-distribution-tracking
tags: [argument-distribution, tool-monitoring, distribution-shift, prompt-regression, behavioral-drift, statistical-monitoring]
symptoms:
  - "Search tool called correctly in count but always with the same query after a prompt change"
  - "Date argument drifts to today's date in 95% of calls — contextual dates ignored"
  - "LLM starts omitting optional arguments entirely after a model upgrade with no alert"
  - "Argument value entropy drops from 3.5 bits to 0.8 bits with no detection"
  - "No baseline of what 'normal' argument distributions look like per tool"
---

## Why This Happens

Tool call frequency is easy to measure; argument quality is not. A tool called 100 times is counted the same whether it was called with 100 diverse, contextually appropriate arguments or 100 identical copy-pasted arguments. Argument distribution tracking requires sampling argument values, computing diversity metrics (entropy, cardinality, length distribution), and comparing current distributions against a baseline. A significant drop in argument diversity or a shift in argument length distribution signals a prompt regression even when call counts look normal.

## Solution 1: Argument Sample Collector

```python
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Deque, Dict, Optional, Tuple


@dataclass
class ArgumentSample:
    tool_name: str
    arg_name: str
    value_repr: str           # string representation, truncated
    value_length: int
    value_type: str
    timestamp: float = field(default_factory=time.time)


class ArgumentSampleCollector:
    """
    Collects argument value samples for each (tool_name, arg_name) pair.
    Stores value representations in a bounded sliding-window deque.
    """

    def __init__(
        self,
        max_samples_per_arg: int = 1000,
        sample_rate: float = 1.0,          # 1.0 = 100%, 0.1 = 10%
    ):
        self._max = max_samples_per_arg
        self._rate = sample_rate
        self._samples: Dict[Tuple[str, str], Deque[ArgumentSample]] = {}
        self._lock = Lock()

    def record(self, tool_name: str, args: Dict[str, Any]) -> None:
        import random
        if random.random() > self._rate:
            return
        with self._lock:
            for arg_name, value in args.items():
                key = (tool_name, arg_name)
                if key not in self._samples:
                    self._samples[key] = deque(maxlen=self._max)
                repr_val = str(value)[:200]
                self._samples[key].append(ArgumentSample(
                    tool_name=tool_name,
                    arg_name=arg_name,
                    value_repr=repr_val,
                    value_length=len(repr_val),
                    value_type=type(value).__name__,
                ))

    def get_samples(self, tool_name: str, arg_name: str) -> list:
        with self._lock:
            return list(self._samples.get((tool_name, arg_name), []))

    def all_keys(self) -> list:
        with self._lock:
            return list(self._samples.keys())
```

## Solution 2: Argument Distribution Analyzer

```python
import math
from typing import List, Optional


class ArgumentDistributionAnalyzer:
    """
    Computes distribution statistics for a set of argument samples:
    cardinality, entropy, length statistics, and type consistency.
    """

    def analyze(self, samples: List[ArgumentSample]) -> dict:
        if not samples:
            return {"sample_count": 0}

        values = [s.value_repr for s in samples]
        lengths = [s.value_length for s in samples]
        types = [s.value_type for s in samples]

        cardinality = len(set(values))
        entropy = self._entropy(values)
        avg_length = sum(lengths) / len(lengths)
        type_consistency = len(set(types)) == 1

        return {
            "sample_count": len(samples),
            "cardinality": cardinality,
            "cardinality_rate": round(cardinality / len(values), 4),
            "entropy_bits": round(entropy, 3),
            "avg_length": round(avg_length, 1),
            "min_length": min(lengths),
            "max_length": max(lengths),
            "type_consistent": type_consistency,
            "dominant_type": max(set(types), key=types.count),
        }

    @staticmethod
    def _entropy(values: List[str]) -> float:
        if not values:
            return 0.0
        counts: dict = {}
        for v in values:
            counts[v] = counts.get(v, 0) + 1
        total = len(values)
        return -sum((c / total) * math.log2(c / total) for c in counts.values())
```

## Solution 3: Distribution Shift Detector

```python
from typing import Optional


class ArgumentDistributionShiftDetector:
    """
    Compares current argument distribution statistics against a stored
    baseline and flags significant shifts.
    """

    def __init__(
        self,
        entropy_drop_threshold: float = 1.0,      # bits drop = shift
        cardinality_rate_drop: float = 0.3,        # 30% drop in unique rate
        length_change_pct: float = 0.5,            # 50% change in avg length
    ):
        self._entropy_threshold = entropy_drop_threshold
        self._cardinality_threshold = cardinality_rate_drop
        self._length_threshold = length_change_pct
        self._baselines: dict = {}

    def set_baseline(self, tool_name: str, arg_name: str, stats: dict) -> None:
        self._baselines[(tool_name, arg_name)] = stats

    def check(self, tool_name: str, arg_name: str, current_stats: dict) -> dict:
        key = (tool_name, arg_name)
        baseline = self._baselines.get(key)

        if not baseline:
            return {
                "tool_name": tool_name,
                "arg_name": arg_name,
                "status": "no_baseline",
            }

        shifts = []

        # Entropy drop
        baseline_entropy = baseline.get("entropy_bits", 0)
        current_entropy = current_stats.get("entropy_bits", 0)
        entropy_drop = baseline_entropy - current_entropy
        if entropy_drop >= self._entropy_threshold and baseline_entropy > 0.5:
            shifts.append({
                "metric": "entropy",
                "baseline": baseline_entropy,
                "current": current_entropy,
                "drop": round(entropy_drop, 3),
            })

        # Cardinality rate drop
        baseline_card = baseline.get("cardinality_rate", 0)
        current_card = current_stats.get("cardinality_rate", 0)
        if baseline_card > 0.1 and current_card < baseline_card * (1 - self._cardinality_threshold):
            shifts.append({
                "metric": "cardinality_rate",
                "baseline": baseline_card,
                "current": current_card,
            })

        # Length change
        baseline_len = baseline.get("avg_length", 0)
        current_len = current_stats.get("avg_length", 0)
        if baseline_len > 5:
            change = abs(current_len - baseline_len) / baseline_len
            if change >= self._length_threshold:
                shifts.append({
                    "metric": "avg_length",
                    "baseline": baseline_len,
                    "current": current_len,
                    "change_pct": round(change * 100, 1),
                })

        return {
            "tool_name": tool_name,
            "arg_name": arg_name,
            "status": "shifted" if shifts else "ok",
            "shifts": shifts,
        }
```

## Solution 4: Argument Distribution Monitor

```python
import time
from typing import List


class ArgumentDistributionMonitor:
    """
    Periodically analyzes collected argument samples and checks for
    distribution shifts across all tracked (tool, arg) pairs.
    """

    def __init__(
        self,
        collector: ArgumentSampleCollector,
        analyzer: ArgumentDistributionAnalyzer,
        shift_detector: ArgumentDistributionShiftDetector,
    ):
        self._collector = collector
        self._analyzer = analyzer
        self._detector = shift_detector

    def build_baselines(self) -> dict:
        """
        Computes baselines from current samples. Call once after a
        warm-up period of normal operation.
        """
        baselines = {}
        for tool_name, arg_name in self._collector.all_keys():
            samples = self._collector.get_samples(tool_name, arg_name)
            stats = self._analyzer.analyze(samples)
            self._detector.set_baseline(tool_name, arg_name, stats)
            baselines[(tool_name, arg_name)] = stats
        return baselines

    def check_all(self) -> List[dict]:
        results = []
        for tool_name, arg_name in self._collector.all_keys():
            samples = self._collector.get_samples(tool_name, arg_name)
            if len(samples) < 20:
                continue
            stats = self._analyzer.analyze(samples)
            check = self._detector.check(tool_name, arg_name, stats)
            check["current_stats"] = stats
            results.append(check)
        return results

    def shifted_args(self) -> List[dict]:
        return [r for r in self.check_all() if r.get("status") == "shifted"]
```

## Solution 5: Instrumented Tool Call Recorder

```python
from typing import Any, Callable, Dict, Optional


class DistributionInstrumentedToolExecutor:
    """
    Wraps tool calls and records argument samples for distribution tracking.
    """

    def __init__(
        self,
        collector: ArgumentSampleCollector,
        monitor: ArgumentDistributionMonitor,
        check_every_n: int = 100,
        shift_alert_fn: Optional[Callable[[list], None]] = None,
    ):
        self._collector = collector
        self._monitor = monitor
        self._check_every = check_every_n
        self._alert_fn = shift_alert_fn
        self._call_count = 0

    async def execute(
        self,
        tool_name: str,
        tool_fn: Callable,
        **kwargs: Any,
    ) -> Any:
        self._collector.record(tool_name, kwargs)
        result = await tool_fn(**kwargs)
        self._call_count += 1
        if self._call_count % self._check_every == 0:
            shifted = self._monitor.shifted_args()
            if shifted and self._alert_fn:
                self._alert_fn(shifted)
        return result
```

## Solution 6: Argument Distribution Dashboard

```python
import time


class ArgumentDistributionDashboard:
    """
    Renders per-argument distribution statistics and shift alerts.
    """

    def __init__(self, monitor: ArgumentDistributionMonitor):
        self._monitor = monitor

    def render(self) -> dict:
        all_checks = self._monitor.check_all()
        shifted = [c for c in all_checks if c.get("status") == "shifted"]
        return {
            "generated_at": time.time(),
            "tracked_args": len(all_checks),
            "shifted_args": len(shifted),
            "shifts": shifted,
            "all_stats": [
                {
                    "tool": c["tool_name"],
                    "arg": c["arg_name"],
                    "status": c["status"],
                    "entropy": c.get("current_stats", {}).get("entropy_bits"),
                    "cardinality_rate": c.get("current_stats", {}).get("cardinality_rate"),
                }
                for c in all_checks
            ],
        }
```

## Comparison

| Approach | Sample Collection | Entropy Tracking | Shift Detection | Auto-Alert | Dashboard |
|---|---|---|---|---|---|
| ArgumentSampleCollector | Yes (sliding window) | No | No | No | No |
| ArgumentDistributionAnalyzer | No | Yes | No | No | No |
| ArgumentDistributionShiftDetector | No | Via analyzer | Yes (3 metrics) | No | No |
| ArgumentDistributionMonitor | Via collector | Via analyzer | Via detector | No | No |
| DistributionInstrumentedToolExecutor | Via collector | Via monitor | Via monitor | Yes | No |
| ArgumentDistributionDashboard | No | No | No | No | Yes |

**Best for production**: Call `build_baselines()` 24 hours after a stable deployment — this captures normal argument diversity before monitoring for drift. Use `sample_rate=0.1` for high-volume tools (>1000 calls/hour) to keep memory bounded while still detecting distribution changes. Alert on entropy drops above 1.5 bits: this corresponds to argument diversity collapsing from ~8 distinct values to ~2, which almost always indicates a prompt regression. Track `cardinality_rate` for free-text arguments like search queries — a drop from 0.9 to 0.3 means the LLM is reusing the same queries rather than generating contextually specific ones.
