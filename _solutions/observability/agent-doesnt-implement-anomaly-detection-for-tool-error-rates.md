---
title: "Agent Doesn't Implement Anomaly Detection for Tool Error Rates"
description: "Agents that only alert on absolute error counts miss gradual degradation: a tool that normally fails 0.5% of the time silently increasing to 8% causes downstream quality problems long before the alert threshold triggers. Implement anomaly detection that baselines per-tool error rates over a rolling window, computes z-scores against the baseline, and alerts when current error rate deviates significantly from normal — even if the absolute count is low."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-anomaly-detection-for-tool-error-rates
tags: [anomaly-detection, error-rate, tool-reliability, z-score, baseline, statistical-alerting]
symptoms:
  - "Tool error rate doubles from 1% to 2% but no alert fires because the threshold is 10%"
  - "Gradual degradation of a search tool goes unnoticed for days because absolute counts look normal"
  - "Alerts only fire during full outages — partial degradation is invisible"
  - "No distinction between a tool that always errors at 5% vs. one that suddenly jumped to 5%"
  - "Cannot tell from dashboards whether current error rates are normal or anomalous for this time of day"
---

## Why This Happens

Fixed-threshold alerting (alert if error_rate > 10%) misses gradual degradation and has high false-positive rates for tools with naturally variable error rates. Statistical anomaly detection computes a baseline mean and standard deviation from historical observations, then alerts when the current rate is more than N standard deviations from baseline. This detects both gradual drift and sudden spikes relative to what is normal for each specific tool, without requiring a separate threshold per tool.

## Solution 1: Tool Error Sample

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ToolErrorSample:
    tool_name: str
    total_calls: int
    error_calls: int
    window_start: float
    window_end: float = field(default_factory=time.time)

    @property
    def error_rate(self) -> float:
        return self.error_calls / max(self.total_calls, 1)

    @property
    def window_duration_seconds(self) -> float:
        return self.window_end - self.window_start
```

## Solution 2: Per-Tool Error Rate Accumulator

```python
import time
from collections import defaultdict, deque
from typing import Deque, Dict, Optional, Tuple


class PerToolErrorRateAccumulator:
    """
    Accumulates call/error counts per tool in a sliding time window.
    Computes error rate samples at configurable intervals.
    """

    def __init__(
        self,
        sample_window_seconds: float = 60.0,
        max_samples_per_tool: int = 500,
    ):
        self._sample_window = sample_window_seconds
        self._max_samples = max_samples_per_tool
        # tool_name -> deque of (timestamp, is_error)
        self._events: Dict[str, Deque[Tuple[float, bool]]] = defaultdict(deque)

    def record(self, tool_name: str, is_error: bool) -> None:
        self._events[tool_name].append((time.time(), is_error))
        events = self._events[tool_name]
        if len(events) > self._max_samples:
            events.popleft()

    def _trim(self, tool_name: str) -> None:
        cutoff = time.time() - self._sample_window
        events = self._events[tool_name]
        while events and events[0][0] < cutoff:
            events.popleft()

    def current_rate(self, tool_name: str) -> Optional[float]:
        self._trim(tool_name)
        events = list(self._events[tool_name])
        if not events:
            return None
        errors = sum(1 for _, is_error in events if is_error)
        return errors / len(events)

    def sample(self, tool_name: str) -> Optional[ToolErrorSample]:
        self._trim(tool_name)
        events = list(self._events[tool_name])
        if len(events) < 5:
            return None
        errors = sum(1 for _, is_error in events if is_error)
        return ToolErrorSample(
            tool_name=tool_name,
            total_calls=len(events),
            error_calls=errors,
            window_start=events[0][0],
            window_end=events[-1][0],
        )

    def all_tool_names(self) -> list:
        return list(self._events.keys())
```

## Solution 3: Baseline Builder

```python
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional


@dataclass
class ErrorRateBaseline:
    tool_name: str
    mean: float
    std_dev: float
    sample_count: int
    updated_at: float = field(default_factory=time.time)

    def z_score(self, current_rate: float) -> float:
        if self.std_dev == 0:
            return 0.0
        return (current_rate - self.mean) / self.std_dev

    def is_anomalous(self, current_rate: float, z_threshold: float = 3.0) -> bool:
        return self.z_score(current_rate) > z_threshold


class ErrorRateBaselineBuilder:
    """
    Maintains a rolling history of error rate samples per tool
    and computes mean/std_dev baselines for anomaly detection.
    """

    def __init__(self, history_size: int = 100):
        self._history_size = history_size
        self._histories: Dict[str, Deque[float]] = {}

    def add_sample(self, sample: ToolErrorSample) -> None:
        tool = sample.tool_name
        if tool not in self._histories:
            self._histories[tool] = deque(maxlen=self._history_size)
        self._histories[tool].append(sample.error_rate)

    def build(self, tool_name: str) -> Optional[ErrorRateBaseline]:
        history = list(self._histories.get(tool_name, []))
        if len(history) < 10:
            return None
        mean = sum(history) / len(history)
        variance = sum((x - mean) ** 2 for x in history) / len(history)
        std_dev = math.sqrt(variance)
        return ErrorRateBaseline(
            tool_name=tool_name,
            mean=round(mean, 6),
            std_dev=round(std_dev, 6),
            sample_count=len(history),
        )

    def all_baselines(self) -> List[ErrorRateBaseline]:
        result = []
        for tool_name in self._histories:
            baseline = self.build(tool_name)
            if baseline:
                result.append(baseline)
        return result
```

## Solution 4: Anomaly Detector

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AnomalyEvent:
    tool_name: str
    current_rate: float
    baseline_mean: float
    baseline_std: float
    z_score: float
    severity: str   # "warning" | "critical"
    detected_at: float = field(default_factory=time.time)


class ToolErrorRateAnomalyDetector:
    """
    Computes z-scores for current error rates against baselines
    and emits AnomalyEvents when thresholds are crossed.
    """

    def __init__(
        self,
        accumulator: PerToolErrorRateAccumulator,
        baseline_builder: ErrorRateBaselineBuilder,
        warning_z: float = 2.5,
        critical_z: float = 4.0,
        min_calls_to_evaluate: int = 10,
    ):
        self._accumulator = accumulator
        self._builder = baseline_builder
        self._warning_z = warning_z
        self._critical_z = critical_z
        self._min_calls = min_calls_to_evaluate

    def detect(self) -> List[AnomalyEvent]:
        anomalies = []
        for tool_name in self._accumulator.all_tool_names():
            sample = self._accumulator.sample(tool_name)
            if not sample or sample.total_calls < self._min_calls:
                continue

            # Update baseline with this sample
            self._builder.add_sample(sample)

            baseline = self._builder.build(tool_name)
            if not baseline:
                continue

            z = baseline.z_score(sample.error_rate)
            if z >= self._critical_z:
                severity = "critical"
            elif z >= self._warning_z:
                severity = "warning"
            else:
                continue

            anomalies.append(AnomalyEvent(
                tool_name=tool_name,
                current_rate=round(sample.error_rate, 6),
                baseline_mean=baseline.mean,
                baseline_std=baseline.std_dev,
                z_score=round(z, 2),
                severity=severity,
            ))
        return anomalies
```

## Solution 5: Anomaly Alert Manager

```python
import time
from typing import Callable, Dict, List, Optional


class AnomalyAlertManager:
    """
    Fires alert callbacks for detected anomalies with per-tool cooldowns.
    Suppresses repeated alerts for the same tool within the cooldown window.
    """

    def __init__(
        self,
        detector: ToolErrorRateAnomalyDetector,
        cooldown_seconds: float = 300.0,
    ):
        self._detector = detector
        self._cooldown = cooldown_seconds
        self._last_alerted: Dict[str, float] = {}
        self._handlers: List[Callable[[AnomalyEvent], None]] = []
        self._fired_events: List[AnomalyEvent] = []

    def add_handler(self, fn: Callable[[AnomalyEvent], None]) -> None:
        self._handlers.append(fn)

    def check_and_alert(self) -> List[AnomalyEvent]:
        anomalies = self._detector.detect()
        fired = []
        now = time.time()

        for event in anomalies:
            last = self._last_alerted.get(event.tool_name, 0)
            if now - last < self._cooldown:
                continue
            self._last_alerted[event.tool_name] = now
            self._fired_events.append(event)
            fired.append(event)
            for handler in self._handlers:
                try:
                    handler(event)
                except Exception:
                    pass

        return fired

    def recent_events(self, window_seconds: float = 3600.0) -> List[AnomalyEvent]:
        cutoff = time.time() - window_seconds
        return [e for e in self._fired_events if e.detected_at >= cutoff]
```

## Solution 6: Anomaly Detection Dashboard

```python
import time
from typing import List


class ToolErrorAnomalyDashboard:
    """
    Combines current error rates, baselines, and recent anomaly events
    into a single observability report.
    """

    def __init__(
        self,
        accumulator: PerToolErrorRateAccumulator,
        baseline_builder: ErrorRateBaselineBuilder,
        alert_manager: AnomalyAlertManager,
    ):
        self._accumulator = accumulator
        self._baseline_builder = baseline_builder
        self._alert_manager = alert_manager

    def render(self) -> dict:
        recent_alerts = self._alert_manager.recent_events(3600)
        baselines = {b.tool_name: b for b in self._baseline_builder.all_baselines()}
        tools = []

        for tool_name in self._accumulator.all_tool_names():
            sample = self._accumulator.sample(tool_name)
            baseline = baselines.get(tool_name)
            tools.append({
                "tool_name": tool_name,
                "current_error_rate": round(sample.error_rate, 4) if sample else None,
                "total_calls_in_window": sample.total_calls if sample else 0,
                "baseline_mean": baseline.mean if baseline else None,
                "baseline_std": baseline.std_dev if baseline else None,
                "z_score": round(baseline.z_score(sample.error_rate), 2)
                           if baseline and sample else None,
                "anomalous": bool(
                    baseline and sample
                    and baseline.z_score(sample.error_rate) >= 2.5
                ),
            })

        tools.sort(key=lambda t: -(t["z_score"] or 0))

        return {
            "generated_at": time.time(),
            "tools": tools,
            "anomalous_tool_count": sum(1 for t in tools if t["anomalous"]),
            "recent_alerts_1h": [
                {
                    "tool": e.tool_name,
                    "z_score": e.z_score,
                    "current_rate": e.current_rate,
                    "severity": e.severity,
                }
                for e in recent_alerts
            ],
        }
```

## Comparison

| Approach | Error Rate Sampling | Baseline Computation | Z-Score Anomaly | Cooldown Alerting | Dashboard |
|---|---|---|---|---|---|
| PerToolErrorRateAccumulator | Yes (sliding window) | No | No | No | No |
| ErrorRateBaselineBuilder | No | Yes (rolling history) | No | No | No |
| ToolErrorRateAnomalyDetector | Via accumulator | Via builder | Yes | No | No |
| AnomalyAlertManager | Via detector | Via detector | Via detector | Yes | No |
| ToolErrorAnomalyDashboard | Via accumulator | Via builder | Via detector | Via manager | Yes |

**Best for production**: Build baselines from at least 100 samples per tool before enabling anomaly alerts — a baseline from 10 samples has high variance and produces false positives. Use `warning_z=2.5` and `critical_z=4.0`: the 2.5σ threshold catches gradual drift (roughly 1.2% false-positive rate for normally distributed data) while 4σ catches sudden spikes with near-zero false positives. Set `cooldown_seconds=300` to prevent alert storms during sustained degradation. Run `AnomalyAlertManager.check_and_alert()` every 30 seconds on a background task — this is lightweight enough to run continuously in production.
