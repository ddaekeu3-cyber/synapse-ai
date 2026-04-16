---
title: "Agent Doesn't Implement Anomaly Detection for Tool Call Frequency"
description: "Agents that do not monitor per-tool call frequency miss runaway execution loops, prompt-injection-driven tool abuse, and downstream API quota exhaustion: a single session calling a search tool 400 times in a minute is abnormal, but without frequency tracking it goes undetected until the API bill arrives. Implement per-tool call frequency anomaly detection with sliding window counters, baseline comparison, and automatic throttling when anomalies are detected."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-anomaly-detection-for-tool-call-frequency
tags: [anomaly-detection, tool-frequency, rate-monitoring, runaway-loop, abuse-detection, frequency-baseline]
symptoms:
  - "Agent enters an infinite tool-call loop — 500 calls before the session is killed"
  - "Prompt injection causes a search tool to be called 50× per session"
  - "No alert when a tool is called 10× more than its historical average"
  - "Downstream API quota exhausted by a single runaway session"
  - "Tool call counts logged but never compared against a baseline or limit"
---

## Why This Happens

Tool call frequency is a first-order signal of agent health. A normally functioning agent calls each tool a predictable number of times per session. Loops, injected instructions, or model confusion cause frequency to spike far above the baseline. Without a frequency monitor, these anomalies surface only as downstream API errors (rate limits, quota exhaustion) or billing surprises — not as real-time operational alerts. Frequency anomaly detection compares current call rate against a rolling baseline and triggers throttling or session termination when the ratio exceeds a threshold.

## Solution 1: Tool Call Frequency Counter

```python
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Deque, Dict, Optional, Tuple


class ToolCallFrequencyCounter:
    """
    Sliding window counter tracking call frequency per tool.
    Returns calls-per-minute for any registered tool.
    """

    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        self._calls: Dict[str, Deque[float]] = {}
        self._lock = Lock()

    def record(self, tool_name: str) -> None:
        now = time.time()
        with self._lock:
            if tool_name not in self._calls:
                self._calls[tool_name] = deque()
            self._calls[tool_name].append(now)
            self._trim(tool_name, now)

    def _trim(self, tool_name: str, now: float) -> None:
        cutoff = now - self._window
        q = self._calls[tool_name]
        while q and q[0] < cutoff:
            q.popleft()

    def rate_per_minute(self, tool_name: str) -> float:
        now = time.time()
        with self._lock:
            if tool_name not in self._calls:
                return 0.0
            self._trim(tool_name, now)
            count = len(self._calls[tool_name])
        return round(count / (self._window / 60.0), 2)

    def total_in_window(self, tool_name: str) -> int:
        now = time.time()
        with self._lock:
            if tool_name not in self._calls:
                return 0
            self._trim(tool_name, now)
            return len(self._calls[tool_name])

    def all_rates(self) -> Dict[str, float]:
        return {name: self.rate_per_minute(name) for name in self._calls}
```

## Solution 2: Per-Tool Frequency Baseline

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, Optional, Tuple


class ToolFrequencyBaselineTracker:
    """
    Maintains a long-window rolling baseline of per-tool call rates.
    Used to compute how anomalous the current rate is relative to history.
    """

    def __init__(self, baseline_window_seconds: float = 3600.0, max_samples: int = 10000):
        self._window = baseline_window_seconds
        self._max = max_samples
        self._samples: Dict[str, Deque[Tuple[float, float]]] = {}
        # tool_name -> deque of (timestamp, rate_per_minute_at_that_time)
        self._lock = Lock()

    def record_rate(self, tool_name: str, rate_per_minute: float) -> None:
        now = time.time()
        with self._lock:
            if tool_name not in self._samples:
                self._samples[tool_name] = deque()
            self._samples[tool_name].append((now, rate_per_minute))
            self._trim(tool_name, now)
            if len(self._samples[tool_name]) > self._max:
                self._samples[tool_name].popleft()

    def _trim(self, tool_name: str, now: float) -> None:
        cutoff = now - self._window
        q = self._samples[tool_name]
        while q and q[0][0] < cutoff:
            q.popleft()

    def baseline_rate(self, tool_name: str) -> Optional[float]:
        with self._lock:
            samples = self._samples.get(tool_name)
            if not samples or len(samples) < 5:
                return None
            rates = sorted(s[1] for s in samples)
            # Use P75 as baseline (robust to occasional spikes)
            idx = int(len(rates) * 0.75)
            return round(rates[min(idx, len(rates) - 1)], 2)
```

## Solution 3: Frequency Anomaly Detector

```python
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class FrequencyAnomalyLevel(str, Enum):
    NORMAL = "normal"
    ELEVATED = "elevated"
    ANOMALOUS = "anomalous"
    CRITICAL = "critical"


@dataclass
class FrequencyAnomalyResult:
    tool_name: str
    current_rate: float
    baseline_rate: Optional[float]
    ratio: Optional[float]
    level: FrequencyAnomalyLevel
    detail: str = ""


class ToolCallFrequencyAnomalyDetector:
    """
    Compares current tool call rate against the baseline and classifies
    the deviation into anomaly levels.
    """

    def __init__(
        self,
        counter: ToolCallFrequencyCounter,
        baseline_tracker: ToolFrequencyBaselineTracker,
        elevated_multiplier: float = 3.0,
        anomalous_multiplier: float = 10.0,
        critical_multiplier: float = 50.0,
        absolute_elevated_rpm: float = 10.0,   # alert if rate > N/min even with no baseline
        absolute_critical_rpm: float = 60.0,
    ):
        self._counter = counter
        self._baseline = baseline_tracker
        self._elevated = elevated_multiplier
        self._anomalous = anomalous_multiplier
        self._critical = critical_multiplier
        self._abs_elevated = absolute_elevated_rpm
        self._abs_critical = absolute_critical_rpm

    def check(self, tool_name: str) -> FrequencyAnomalyResult:
        current = self._counter.rate_per_minute(tool_name)
        baseline = self._baseline.baseline_rate(tool_name)

        if baseline is None:
            # No baseline — use absolute thresholds
            if current >= self._abs_critical:
                level = FrequencyAnomalyLevel.CRITICAL
            elif current >= self._abs_elevated:
                level = FrequencyAnomalyLevel.ELEVATED
            else:
                level = FrequencyAnomalyLevel.NORMAL
            return FrequencyAnomalyResult(
                tool_name=tool_name,
                current_rate=current,
                baseline_rate=None,
                ratio=None,
                level=level,
                detail="no baseline — using absolute thresholds",
            )

        ratio = current / max(baseline, 0.01)

        if ratio >= self._critical:
            level = FrequencyAnomalyLevel.CRITICAL
        elif ratio >= self._anomalous:
            level = FrequencyAnomalyLevel.ANOMALOUS
        elif ratio >= self._elevated:
            level = FrequencyAnomalyLevel.ELEVATED
        else:
            level = FrequencyAnomalyLevel.NORMAL

        return FrequencyAnomalyResult(
            tool_name=tool_name,
            current_rate=current,
            baseline_rate=baseline,
            ratio=round(ratio, 2),
            level=level,
            detail=f"{current:.1f} rpm vs baseline {baseline:.1f} rpm ({ratio:.1f}×)",
        )

    def check_all(self) -> list:
        return [self.check(name) for name in self._counter._calls]
```

## Solution 4: Frequency-Based Session Throttler

```python
import asyncio
from typing import Callable, Optional


class FrequencyBasedSessionThrottler:
    """
    Monitors tool call frequency per session and applies throttling
    or termination when anomaly levels cross configured thresholds.
    """

    def __init__(
        self,
        detector: ToolCallFrequencyAnomalyDetector,
        on_throttle: Optional[Callable[[str, FrequencyAnomalyResult], None]] = None,
        on_terminate: Optional[Callable[[str, FrequencyAnomalyResult], None]] = None,
        throttle_level: FrequencyAnomalyLevel = FrequencyAnomalyLevel.ANOMALOUS,
        terminate_level: FrequencyAnomalyLevel = FrequencyAnomalyLevel.CRITICAL,
        throttle_delay_seconds: float = 2.0,
    ):
        self._detector = detector
        self._on_throttle = on_throttle
        self._on_terminate = on_terminate
        self._throttle_level = throttle_level
        self._terminate_level = terminate_level
        self._delay = throttle_delay_seconds

    LEVEL_ORDER = {
        FrequencyAnomalyLevel.NORMAL: 0,
        FrequencyAnomalyLevel.ELEVATED: 1,
        FrequencyAnomalyLevel.ANOMALOUS: 2,
        FrequencyAnomalyLevel.CRITICAL: 3,
    }

    async def check_and_enforce(self, session_id: str, tool_name: str) -> bool:
        """
        Returns True if the tool call is allowed, False if it should be blocked.
        May introduce a delay for throttled sessions.
        """
        result = self._detector.check(tool_name)
        level_val = self.LEVEL_ORDER[result.level]

        if level_val >= self.LEVEL_ORDER[self._terminate_level]:
            if self._on_terminate:
                self._on_terminate(session_id, result)
            return False

        if level_val >= self.LEVEL_ORDER[self._throttle_level]:
            if self._on_throttle:
                self._on_throttle(session_id, result)
            await asyncio.sleep(self._delay)

        return True
```

## Solution 5: Frequency Anomaly Alert Logger

```python
import time
from typing import List


class FrequencyAnomalyAlertLogger:
    def __init__(self, max_records: int = 5000):
        self._records: List[dict] = []
        self._max = max_records

    def record(self, session_id: str, result: FrequencyAnomalyResult) -> None:
        if result.level == FrequencyAnomalyLevel.NORMAL:
            return
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "session_id": session_id,
            "tool_name": result.tool_name,
            "level": result.level.value,
            "current_rate": result.current_rate,
            "baseline_rate": result.baseline_rate,
            "ratio": result.ratio,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        by_tool: dict = {}
        by_level: dict = {}
        for r in recent:
            by_tool[r["tool_name"]] = by_tool.get(r["tool_name"], 0) + 1
            by_level[r["level"]] = by_level.get(r["level"], 0) + 1
        return {
            "window_seconds": window_seconds,
            "anomalies": len(recent),
            "by_tool": dict(sorted(by_tool.items(), key=lambda x: -x[1])),
            "by_level": by_level,
        }
```

## Solution 6: Tool Frequency Anomaly Dashboard

```python
import time


class ToolFrequencyAnomalyDashboard:
    def __init__(
        self,
        counter: ToolCallFrequencyCounter,
        detector: ToolCallFrequencyAnomalyDetector,
        alert_logger: FrequencyAnomalyAlertLogger,
    ):
        self._counter = counter
        self._detector = detector
        self._logger = alert_logger

    def render(self) -> dict:
        assessments = self._detector.check_all()
        return {
            "generated_at": time.time(),
            "current_rates": self._counter.all_rates(),
            "anomaly_assessments": [
                {
                    "tool": a.tool_name,
                    "rate_rpm": a.current_rate,
                    "baseline_rpm": a.baseline_rate,
                    "ratio": a.ratio,
                    "level": a.level.value,
                }
                for a in assessments
            ],
            "alert_summary": self._logger.summary(3600.0),
        }
```

## Comparison

| Approach | Per-Tool Rate | Baseline Comparison | Anomaly Levels | Throttling | Dashboard |
|---|---|---|---|---|---|
| ToolCallFrequencyCounter | Yes (sliding) | No | No | No | No |
| ToolFrequencyBaselineTracker | Via counter | Yes (P75) | No | No | No |
| ToolCallFrequencyAnomalyDetector | Via counter | Via baseline | Yes (4 levels) | No | No |
| FrequencyBasedSessionThrottler | Via detector | Via detector | Yes | Yes (delay/block) | No |
| ToolFrequencyAnomalyDashboard | No | No | No | No | Yes |

**Best for production**: Set `absolute_critical_rpm=60` as a safety floor — any tool called more than once per second is almost certainly in a loop. Build the baseline over the first 1000 sessions before enabling ratio-based detection; until then, rely on absolute thresholds. Wire `on_terminate` to session cancellation and a PagerDuty alert — a CRITICAL frequency event means the agent is actively consuming quota at a harmful rate. Monitor `by_tool` from the alert logger: if one tool accounts for 80%+ of anomalies, that tool's invocation logic is likely the root cause.
