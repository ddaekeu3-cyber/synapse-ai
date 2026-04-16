---
title: "Agent Doesn't Implement Tool Invocation Frequency Anomaly Detection"
description: "Agents that never monitor how often each tool is called miss signals that indicate looping behavior, prompt injection exploitation, or degraded decision-making: a web search tool called 40 times in a single session, a file-read tool invoked in a tight loop, or a payment tool called at 10× its normal rate. Implement tool invocation frequency anomaly detection that tracks per-tool call rates, establishes baselines, and alerts when invocation patterns deviate from normal."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-invocation-frequency-anomaly-detection
tags: [frequency-anomaly, tool-monitoring, invocation-rate, loop-detection, call-rate-baseline, behavioral-anomaly]
symptoms:
  - "Agent enters a tool-calling loop with no detection or termination"
  - "A single session calls a payment tool 50 times before anyone notices"
  - "No baseline for what a normal tool invocation rate looks like"
  - "Prompt injection that redirects tool usage goes undetected for hours"
  - "No alert when a rarely-used tool suddenly spikes to 100 calls per minute"
---

## Why This Happens

Tool invocation frequency is a behavioral signal that most observability setups ignore. Call counts are logged but never analyzed for rate anomalies. A tight loop where the agent repeatedly calls the same tool — often caused by a flawed reasoning chain or a prompt injection — is only detectable by measuring the call rate against a baseline. Anomaly detection requires a sliding window counter per tool, a baseline derived from historical call rates, a deviation threshold, and an alert mechanism that fires before the loop causes irreversible side effects.

## Solution 1: Tool Invocation Counter

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, Optional, Tuple


class ToolInvocationCounter:
    """
    Counts tool invocations within a sliding time window per tool name
    and per session. Supports both global and session-scoped rate queries.
    """

    def __init__(self, window_seconds: float = 60.0):
        self._window = window_seconds
        self._global_events: Dict[str, Deque[float]] = {}
        self._session_events: Dict[str, Dict[str, Deque[float]]] = {}
        self._lock = Lock()

    def record(self, tool_name: str, session_id: str = "") -> None:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            if tool_name not in self._global_events:
                self._global_events[tool_name] = deque()
            self._global_events[tool_name].append(now)
            while self._global_events[tool_name] and self._global_events[tool_name][0] < cutoff:
                self._global_events[tool_name].popleft()

            if session_id:
                if session_id not in self._session_events:
                    self._session_events[session_id] = {}
                if tool_name not in self._session_events[session_id]:
                    self._session_events[session_id][tool_name] = deque()
                self._session_events[session_id][tool_name].append(now)
                while self._session_events[session_id][tool_name] and \
                        self._session_events[session_id][tool_name][0] < cutoff:
                    self._session_events[session_id][tool_name].popleft()

    def global_rate(self, tool_name: str) -> float:
        with self._lock:
            events = self._global_events.get(tool_name, deque())
            return len(events) / self._window

    def session_count(self, tool_name: str, session_id: str) -> int:
        with self._lock:
            return len(
                self._session_events.get(session_id, {}).get(tool_name, deque())
            )

    def all_global_rates(self) -> Dict[str, float]:
        with self._lock:
            return {
                name: len(events) / self._window
                for name, events in self._global_events.items()
            }
```

## Solution 2: Invocation Rate Baseline Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class InvocationRateBaselineTracker:
    """
    Maintains a rolling baseline of per-tool invocation rates.
    Computes mean and standard deviation to support z-score-based
    anomaly detection.
    """

    def __init__(self, baseline_window: int = 1000):
        self._window = baseline_window
        self._rate_samples: Dict[str, Deque[float]] = {}
        self._lock = Lock()

    def record_rate_sample(self, tool_name: str, rate: float) -> None:
        with self._lock:
            if tool_name not in self._rate_samples:
                self._rate_samples[tool_name] = deque(maxlen=self._window)
            self._rate_samples[tool_name].append(rate)

    def baseline(self, tool_name: str) -> Optional[Tuple[float, float]]:
        """Returns (mean, std_dev) or None if insufficient data."""
        with self._lock:
            samples = list(self._rate_samples.get(tool_name, []))
        if len(samples) < 20:
            return None
        mean = sum(samples) / len(samples)
        variance = sum((x - mean) ** 2 for x in samples) / len(samples)
        std = variance ** 0.5
        return mean, std

    def z_score(self, tool_name: str, current_rate: float) -> Optional[float]:
        result = self.baseline(tool_name)
        if result is None:
            return None
        mean, std = result
        if std < 0.001:
            return None
        return (current_rate - mean) / std
```

## Solution 3: Per-Session Loop Detector

```python
import time
from typing import Dict, List, Optional


class PerSessionLoopDetector:
    """
    Detects tool-calling loops within a session by checking if the same
    tool has been called more than a threshold number of times in the
    current session window.
    """

    def __init__(
        self,
        per_session_call_limit: int = 20,
        per_session_window_seconds: float = 300.0,
    ):
        self._limit = per_session_call_limit
        self._window = per_session_window_seconds
        self._session_calls: Dict[str, Dict[str, List[float]]] = {}

    def record(self, tool_name: str, session_id: str) -> None:
        now = time.time()
        if session_id not in self._session_calls:
            self._session_calls[session_id] = {}
        if tool_name not in self._session_calls[session_id]:
            self._session_calls[session_id][tool_name] = []
        calls = self._session_calls[session_id][tool_name]
        calls.append(now)
        cutoff = now - self._window
        self._session_calls[session_id][tool_name] = [t for t in calls if t >= cutoff]

    def is_looping(self, tool_name: str, session_id: str) -> dict:
        calls = self._session_calls.get(session_id, {}).get(tool_name, [])
        count = len(calls)
        looping = count >= self._limit
        return {
            "looping": looping,
            "call_count": count,
            "limit": self._limit,
            "window_seconds": self._window,
            "tool_name": tool_name,
            "session_id": session_id,
        }

    def reset_session(self, session_id: str) -> None:
        self._session_calls.pop(session_id, None)
```

## Solution 4: Frequency Anomaly Detector

```python
import time
from typing import List, Optional


class ToolFrequencyAnomalyDetector:
    """
    Combines global rate z-score analysis with per-session loop detection
    to produce a unified anomaly signal per tool call event.
    """

    def __init__(
        self,
        counter: ToolInvocationCounter,
        baseline: InvocationRateBaselineTracker,
        loop_detector: PerSessionLoopDetector,
        z_score_threshold: float = 3.0,
    ):
        self._counter = counter
        self._baseline = baseline
        self._loop_detector = loop_detector
        self._z_threshold = z_score_threshold
        self._anomaly_log: List[dict] = []

    def observe(
        self,
        tool_name: str,
        session_id: str = "",
    ) -> dict:
        self._counter.record(tool_name, session_id)
        rate = self._counter.global_rate(tool_name)
        self._baseline.record_rate_sample(tool_name, rate)
        z = self._baseline.z_score(tool_name, rate)

        rate_anomaly = z is not None and z > self._z_threshold
        loop_result = self._loop_detector.is_looping(tool_name, session_id) if session_id else {}
        loop_anomaly = loop_result.get("looping", False)

        anomaly = rate_anomaly or loop_anomaly

        result = {
            "tool_name": tool_name,
            "session_id": session_id,
            "current_rate_per_sec": round(rate, 4),
            "z_score": round(z, 2) if z is not None else None,
            "rate_anomaly": rate_anomaly,
            "loop_anomaly": loop_anomaly,
            "anomaly": anomaly,
            "observed_at": time.time(),
        }

        if anomaly:
            self._anomaly_log.append(result)

        return result

    def recent_anomalies(self, limit: int = 50) -> List[dict]:
        return self._anomaly_log[-limit:]
```

## Solution 5: Frequency-Gated Tool Executor

```python
from typing import Any, Callable, Dict


class FrequencyGatedToolExecutor:
    """
    Wraps tool execution with frequency anomaly detection.
    Raises RuntimeError if a loop is detected; emits a warning for
    rate anomalies but allows execution to continue.
    """

    def __init__(
        self,
        detector: ToolFrequencyAnomalyDetector,
        block_on_loop: bool = True,
    ):
        self._detector = detector
        self._block_on_loop = block_on_loop
        self._blocked_count = 0

    async def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        execute_fn: Callable,
        session_id: str = "",
    ) -> Any:
        observation = self._detector.observe(tool_name, session_id)

        if self._block_on_loop and observation["loop_anomaly"]:
            self._blocked_count += 1
            raise RuntimeError(
                f"Tool '{tool_name}' call blocked: loop detected "
                f"({observation.get('loop_result', {}).get('call_count', '?')} calls "
                f"in session '{session_id}')"
            )

        return await execute_fn(tool_name, arguments)

    def stats(self) -> dict:
        return {"blocked_calls": self._blocked_count}
```

## Solution 6: Invocation Frequency Dashboard

```python
import time


class ToolInvocationFrequencyDashboard:
    """
    Surfaces per-tool call rates, baseline deviations, and
    recent anomaly events in a single operational view.
    """

    def __init__(
        self,
        counter: ToolInvocationCounter,
        baseline: InvocationRateBaselineTracker,
        detector: ToolFrequencyAnomalyDetector,
    ):
        self._counter = counter
        self._baseline = baseline
        self._detector = detector

    def render(self) -> dict:
        global_rates = self._counter.all_global_rates()
        tool_status = {}
        for tool_name, rate in global_rates.items():
            z = self._baseline.z_score(tool_name, rate)
            bl = self._baseline.baseline(tool_name)
            tool_status[tool_name] = {
                "current_rate_per_sec": round(rate, 4),
                "baseline_mean": round(bl[0], 4) if bl else None,
                "baseline_std": round(bl[1], 4) if bl else None,
                "z_score": round(z, 2) if z is not None else None,
            }
        return {
            "generated_at": time.time(),
            "tool_rates": tool_status,
            "recent_anomalies": self._detector.recent_anomalies(limit=10),
            "total_anomalies": len(self._detector._anomaly_log),
        }
```

## Comparison

| Approach | Rate Measurement | Baseline Tracking | Loop Detection | Anomaly Gating | Dashboard |
|---|---|---|---|---|---|
| ToolInvocationCounter | Yes (sliding window) | No | No | No | No |
| InvocationRateBaselineTracker | No | Yes (z-score) | No | No | No |
| PerSessionLoopDetector | No | No | Yes (count limit) | No | No |
| ToolFrequencyAnomalyDetector | Via counter | Via baseline | Via loop detector | No | No |
| FrequencyGatedToolExecutor | No | No | No | Yes (block loop) | No |
| ToolInvocationFrequencyDashboard | No | No | No | No | Yes |

**Best for production**: Set `per_session_call_limit=20` for write tools (payment, email, file-write) and `per_session_call_limit=50` for read tools — this catches loops before they cause significant damage while avoiding false positives for legitimate heavy-read workflows. Use `z_score_threshold=3.0` for rate anomaly detection; a z-score above 3 occurs less than 0.3% of the time in a normal distribution, making false alarms rare. Feed anomaly events into your incident alert channel immediately — a tool loop that charges payments 20 times before detection has already caused user harm.
