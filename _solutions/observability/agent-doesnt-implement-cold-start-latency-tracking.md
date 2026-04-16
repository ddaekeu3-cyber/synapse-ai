---
title: "Agent Doesn't Implement Cold Start Latency Tracking"
description: "Agents deployed as serverless functions or containers that scale to zero experience cold start latency — the overhead of initializing model clients, loading tool registries, and establishing connection pools on the first request after idle. Without tracking cold starts separately, cold-start latency inflates P99 metrics and obscures whether high tail latency is an infrastructure problem or an application problem. Implement cold start detection and dedicated latency tracking for initialization phases."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-cold-start-latency-tracking
tags: [cold-start, latency-tracking, initialization, serverless, container-startup, p99-latency]
symptoms:
  - "P99 latency is 10× median but median is fine — cold starts are inflating the tail"
  - "No way to distinguish first-request latency from steady-state latency in dashboards"
  - "On-call engineers investigate P99 spikes that turn out to be normal cold starts"
  - "Initialization code timing is never measured — only post-init request latency is tracked"
  - "Cannot determine whether cold start duration has regressed after a dependency update"
---

## Why This Happens

Observability tools measure what happens after the agent is running. Cold start latency — the time spent importing modules, initializing clients, loading configurations, and warming caches — occurs before the first request is handled, so it falls outside the standard request latency histogram. Without an explicit cold start detection and timing mechanism, the first request's latency absorbs all initialization overhead and lands in the P99 bucket with no label distinguishing it from a genuine application slowdown. Tracking cold starts as a first-class metric allows them to be filtered from steady-state SLOs and optimized independently.

## Solution 1: Cold Start Detector

```python
import os
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ColdStartRecord:
    instance_id: str
    process_start_time: float        # time.time() at process boot
    first_request_time: Optional[float] = None
    cold_start_duration_ms: Optional[float] = None
    is_cold: bool = True
    environment: str = ""            # "lambda" | "cloud_run" | "k8s" | "local"
    metadata: dict = field(default_factory=dict)


class ColdStartDetector:
    """
    Detects whether the current request is a cold start by tracking
    whether a first request has been seen in this process lifetime.
    Records the duration from process boot to first request.
    """

    def __init__(self, instance_id: str = "", environment: str = ""):
        self._process_start = time.time()
        self._instance_id = instance_id or os.getenv("INSTANCE_ID", f"pid-{os.getpid()}")
        self._environment = environment or self._detect_environment()
        self._record = ColdStartRecord(
            instance_id=self._instance_id,
            process_start_time=self._process_start,
            environment=self._environment,
        )
        self._warm = False

    @staticmethod
    def _detect_environment() -> str:
        if os.getenv("AWS_LAMBDA_FUNCTION_NAME"):
            return "lambda"
        if os.getenv("K_SERVICE"):
            return "cloud_run"
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            return "k8s"
        return "local"

    def check(self) -> ColdStartRecord:
        """Call at the start of each request. Returns the cold start record."""
        if not self._warm:
            now = time.time()
            self._record.first_request_time = now
            self._record.cold_start_duration_ms = round(
                (now - self._process_start) * 1000, 2
            )
            self._record.is_cold = True
            self._warm = True
        else:
            self._record.is_cold = False
        return self._record

    def is_warm(self) -> bool:
        return self._warm
```

## Solution 2: Initialization Phase Timer

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class InitPhaseRecord:
    phase_name: str
    duration_ms: float
    success: bool
    error: Optional[str] = None


class InitializationPhaseTimer:
    """
    Times discrete initialization phases (model client init,
    tool registry load, cache warm-up, etc.) and records their
    individual durations for cold start breakdown analysis.
    """

    def __init__(self):
        self._phases: List[InitPhaseRecord] = []
        self._active: Dict[str, float] = {}

    def start_phase(self, phase_name: str) -> None:
        self._active[phase_name] = time.time()

    def end_phase(self, phase_name: str, error: Optional[str] = None) -> float:
        start = self._active.pop(phase_name, time.time())
        duration_ms = round((time.time() - start) * 1000, 2)
        self._phases.append(InitPhaseRecord(
            phase_name=phase_name,
            duration_ms=duration_ms,
            success=error is None,
            error=error,
        ))
        return duration_ms

    def total_init_ms(self) -> float:
        return round(sum(p.duration_ms for p in self._phases), 2)

    def slowest_phase(self) -> Optional[InitPhaseRecord]:
        if not self._phases:
            return None
        return max(self._phases, key=lambda p: p.duration_ms)

    def report(self) -> dict:
        return {
            "total_init_ms": self.total_init_ms(),
            "phase_count": len(self._phases),
            "phases": [
                {
                    "name": p.phase_name,
                    "duration_ms": p.duration_ms,
                    "success": p.success,
                    "error": p.error,
                }
                for p in self._phases
            ],
            "slowest_phase": self.slowest_phase().phase_name if self.slowest_phase() else None,
        }
```

## Solution 3: Cold Start Metrics Recorder

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class ColdStartMetricsRecorder:
    """
    Accumulates cold start duration observations across deployments.
    Supports percentile queries for SLO tracking of cold start latency.
    """

    def __init__(self, max_records: int = 10000):
        self._max = max_records
        self._records: Deque[Tuple[float, float, str]] = deque()
        # (recorded_at, cold_start_ms, environment)
        self._lock = Lock()

    def record(self, record: ColdStartRecord) -> None:
        if not record.is_cold or record.cold_start_duration_ms is None:
            return
        with self._lock:
            self._records.append((
                time.time(),
                record.cold_start_duration_ms,
                record.environment,
            ))
            if len(self._records) > self._max:
                self._records.popleft()

    def percentile(self, pct: float, window_seconds: float = 3600.0) -> Optional[float]:
        cutoff = time.time() - window_seconds
        with self._lock:
            values = sorted(
                ms for ts, ms, _ in self._records if ts >= cutoff
            )
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        with self._lock:
            recent = [(ts, ms, env) for ts, ms, env in self._records if ts >= cutoff]

        if not recent:
            return {"window_seconds": window_seconds, "cold_starts": 0}

        durations = [ms for _, ms, _ in recent]
        by_env: dict = {}
        for _, ms, env in recent:
            if env not in by_env:
                by_env[env] = []
            by_env[env].append(ms)

        return {
            "window_seconds": window_seconds,
            "cold_starts": len(recent),
            "p50_ms": self.percentile(50, window_seconds),
            "p95_ms": self.percentile(95, window_seconds),
            "p99_ms": self.percentile(99, window_seconds),
            "mean_ms": round(sum(durations) / len(durations), 2),
            "by_environment": {
                env: {
                    "count": len(vals),
                    "mean_ms": round(sum(vals) / len(vals), 2),
                }
                for env, vals in by_env.items()
            },
        }
```

## Solution 4: Cold Start Aware Request Handler

```python
import time
from typing import Any, Callable, Optional


class ColdStartAwareRequestHandler:
    """
    Wraps request handling with cold start detection.
    Tags each request with its cold start status and records
    initialization breakdown on cold requests.
    """

    def __init__(
        self,
        detector: ColdStartDetector,
        metrics_recorder: ColdStartMetricsRecorder,
        init_timer: Optional[InitializationPhaseTimer] = None,
    ):
        self._detector = detector
        self._recorder = metrics_recorder
        self._init_timer = init_timer

    async def handle(
        self,
        request_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> dict:
        cold_record = self._detector.check()

        if cold_record.is_cold:
            self._recorder.record(cold_record)

        start = time.time()
        try:
            result = await request_fn(*args, **kwargs)
            latency_ms = round((time.time() - start) * 1000, 2)
            return {
                "result": result,
                "latency_ms": latency_ms,
                "cold_start": cold_record.is_cold,
                "cold_start_ms": cold_record.cold_start_duration_ms,
                "init_phases": self._init_timer.report() if self._init_timer else None,
            }
        except Exception:
            raise
```

## Solution 5: Cold Start Regression Detector

```python
import time
from typing import Optional


class ColdStartRegressionDetector:
    """
    Compares cold start P95 between two time windows (baseline vs recent)
    to detect regressions introduced by dependency updates or config changes.
    """

    def __init__(
        self,
        recorder: ColdStartMetricsRecorder,
        regression_threshold_pct: float = 25.0,
    ):
        self._recorder = recorder
        self._threshold = regression_threshold_pct / 100.0

    def check_regression(
        self,
        baseline_window_seconds: float = 86400.0,  # 24h baseline
        recent_window_seconds: float = 3600.0,     # last 1h recent
    ) -> dict:
        baseline_p95 = self._recorder.percentile(95, baseline_window_seconds)
        recent_p95 = self._recorder.percentile(95, recent_window_seconds)

        if baseline_p95 is None or recent_p95 is None:
            return {
                "status": "insufficient_data",
                "baseline_p95_ms": baseline_p95,
                "recent_p95_ms": recent_p95,
            }

        change = (recent_p95 - baseline_p95) / max(baseline_p95, 1)
        regressed = change > self._threshold

        return {
            "status": "regression" if regressed else "ok",
            "baseline_p95_ms": baseline_p95,
            "recent_p95_ms": recent_p95,
            "change_pct": round(change * 100, 1),
            "threshold_pct": self._threshold * 100,
            "regressed": regressed,
        }
```

## Solution 6: Cold Start Dashboard

```python
import time


class ColdStartDashboard:
    """
    Combines cold start metrics, initialization phase breakdown,
    and regression analysis into a single operational report.
    """

    def __init__(
        self,
        detector: ColdStartDetector,
        recorder: ColdStartMetricsRecorder,
        regression_detector: ColdStartRegressionDetector,
        init_timer: Optional[InitializationPhaseTimer] = None,
    ):
        self._detector = detector
        self._recorder = recorder
        self._regression = regression_detector
        self._init_timer = init_timer

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "instance": {
                "id": self._detector._instance_id,
                "environment": self._detector._environment,
                "warm": self._detector.is_warm(),
            },
            "cold_start_metrics": self._recorder.summary(window_seconds=3600.0),
            "regression": self._regression.check_regression(),
            "init_phases": self._init_timer.report() if self._init_timer else None,
        }
```

## Comparison

| Approach | First-Request Detection | Phase Breakdown | Percentile Tracking | Regression Detection | Dashboard |
|---|---|---|---|---|---|
| ColdStartDetector | Yes (per-process) | No | No | No | No |
| InitializationPhaseTimer | No | Yes (named phases) | No | No | No |
| ColdStartMetricsRecorder | No | No | Yes (P50/P95/P99) | No | No |
| ColdStartAwareRequestHandler | Via detector | Via init timer | Via recorder | No | No |
| ColdStartRegressionDetector | No | No | Via recorder | Yes | No |
| ColdStartDashboard | No | No | No | No | Yes |

**Best for production**: Wrap every serverless function entry point with `ColdStartAwareRequestHandler` and emit `cold_start=true` as a tag on every metrics data point — this lets dashboards filter cold starts out of steady-state SLO calculations. Use `InitializationPhaseTimer` to time model client init, tool registry load, and DB pool establishment separately: when cold start P95 regresses, the phase breakdown pinpoints which dependency got slower. Set a cold start SLO (e.g., P95 < 3 seconds for Lambda) and alert via `ColdStartRegressionDetector` when a deployment pushes it above the threshold.
