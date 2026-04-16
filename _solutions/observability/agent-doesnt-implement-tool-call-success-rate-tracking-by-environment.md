---
title: "Agent Doesn't Implement Tool Call Success Rate Tracking by Environment"
description: "Agents that aggregate tool call success metrics without environment segmentation cannot distinguish production failures from staging noise, or detect that a tool works in development but fails in production due to network policy, credential differences, or infrastructure configuration. Implement environment-tagged success rate tracking that separates reliability signals by deployment environment and surfaces cross-environment divergence."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-tool-call-success-rate-tracking-by-environment
tags: [success-rate, environment-tagging, production-vs-staging, tool-reliability, cross-environment, deployment-observability]
symptoms:
  - "Success rate dashboard mixes production and staging calls, obscuring real production reliability"
  - "Tool works 100% in staging but fails 30% in production — no metric shows the divergence"
  - "Cannot determine whether a new deployment caused a regression or staging failure is noise"
  - "Alerts fire on staging failures that wake on-call engineers unnecessarily"
  - "No per-environment baseline for what 'normal' tool success rate looks like"
---

## Why This Happens

Tool call success metrics are often aggregated globally. When staging, development, and production calls flow into the same counter, a spike in staging test failures inflates the global failure rate, and production-specific regressions are diluted by high-volume staging success. Environment tagging requires adding a consistent `environment` label to every metric data point at collection time — not as a post-hoc filter — and maintaining separate baselines per environment. Cross-environment comparison surfaces the most actionable signal: a tool that diverges significantly between staging and production is almost certainly failing due to environment-specific configuration, not application logic.

## Solution 1: Environment-Tagged Tool Call Record

```python
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class Environment(str, Enum):
    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"
    TEST = "test"
    UNKNOWN = "unknown"


@dataclass
class ToolCallRecord:
    tool_name: str
    environment: Environment
    success: bool
    latency_ms: float
    session_id: str = ""
    error_type: Optional[str] = None
    error_message: Optional[str] = None
    recorded_at: float = field(default_factory=time.time)

    @classmethod
    def detect_environment(cls) -> Environment:
        env_var = os.getenv("AGENT_ENVIRONMENT", "").lower()
        mapping = {
            "production": Environment.PRODUCTION,
            "prod": Environment.PRODUCTION,
            "staging": Environment.STAGING,
            "stage": Environment.STAGING,
            "development": Environment.DEVELOPMENT,
            "dev": Environment.DEVELOPMENT,
            "test": Environment.TEST,
        }
        return mapping.get(env_var, Environment.UNKNOWN)
```

## Solution 2: Environment-Segmented Success Rate Recorder

```python
import time
from collections import defaultdict, deque
from threading import Lock
from typing import DefaultDict, Deque, Dict, List, Optional, Tuple


class EnvironmentSegmentedSuccessRecorder:
    """
    Records tool call outcomes segmented by environment and tool name.
    Provides per-environment success rates and cross-environment comparison.
    """

    def __init__(self, window_seconds: float = 3600.0, max_records: int = 100000):
        self._window = window_seconds
        self._max = max_records
        self._records: Deque[ToolCallRecord] = deque()
        self._lock = Lock()

    def record(self, record: ToolCallRecord) -> None:
        with self._lock:
            self._records.append(record)
            if len(self._records) > self._max:
                self._records.popleft()

    def success_rate(
        self,
        tool_name: Optional[str] = None,
        environment: Optional[Environment] = None,
        window_seconds: Optional[float] = None,
    ) -> Optional[float]:
        records = self._filter(tool_name, environment, window_seconds)
        if not records:
            return None
        return round(sum(1 for r in records if r.success) / len(records), 4)

    def _filter(
        self,
        tool_name: Optional[str],
        environment: Optional[Environment],
        window_seconds: Optional[float],
    ) -> List[ToolCallRecord]:
        cutoff = time.time() - (window_seconds or self._window)
        with self._lock:
            return [
                r for r in self._records
                if r.recorded_at >= cutoff
                and (tool_name is None or r.tool_name == tool_name)
                and (environment is None or r.environment == environment)
            ]

    def per_environment_summary(
        self,
        tool_name: Optional[str] = None,
        window_seconds: Optional[float] = None,
    ) -> Dict[str, dict]:
        result = {}
        for env in Environment:
            records = self._filter(tool_name, env, window_seconds)
            if not records:
                continue
            successes = sum(1 for r in records if r.success)
            result[env.value] = {
                "calls": len(records),
                "success_rate": round(successes / len(records), 4),
                "failure_count": len(records) - successes,
            }
        return result
```

## Solution 3: Cross-Environment Divergence Detector

```python
from typing import List, Optional


class CrossEnvironmentDivergenceDetector:
    """
    Detects when a tool's success rate in production diverges significantly
    from its rate in staging — signalling an environment-specific failure.
    """

    def __init__(
        self,
        recorder: EnvironmentSegmentedSuccessRecorder,
        divergence_threshold: float = 0.15,
        min_samples: int = 20,
    ):
        self._recorder = recorder
        self._threshold = divergence_threshold
        self._min_samples = min_samples

    def detect(
        self,
        tool_name: str,
        reference_env: Environment = Environment.STAGING,
        target_env: Environment = Environment.PRODUCTION,
        window_seconds: float = 3600.0,
    ) -> dict:
        ref_records = self._recorder._filter(tool_name, reference_env, window_seconds)
        tgt_records = self._recorder._filter(tool_name, target_env, window_seconds)

        if len(ref_records) < self._min_samples or len(tgt_records) < self._min_samples:
            return {
                "status": "insufficient_data",
                "tool_name": tool_name,
                "reference_env": reference_env.value,
                "target_env": target_env.value,
                "reference_samples": len(ref_records),
                "target_samples": len(tgt_records),
            }

        ref_rate = sum(1 for r in ref_records if r.success) / len(ref_records)
        tgt_rate = sum(1 for r in tgt_records if r.success) / len(tgt_records)
        divergence = ref_rate - tgt_rate  # positive = prod worse than staging

        return {
            "status": "diverged" if divergence > self._threshold else "aligned",
            "tool_name": tool_name,
            "reference_env": reference_env.value,
            "reference_success_rate": round(ref_rate, 4),
            "target_env": target_env.value,
            "target_success_rate": round(tgt_rate, 4),
            "divergence": round(divergence, 4),
            "threshold": self._threshold,
        }

    def scan_all_tools(
        self,
        window_seconds: float = 3600.0,
    ) -> List[dict]:
        with self._recorder._lock:
            tools = {r.tool_name for r in self._recorder._records}
        return [
            result
            for tool in tools
            for result in [self.detect(tool, window_seconds=window_seconds)]
            if result["status"] == "diverged"
        ]
```

## Solution 4: Environment-Aware Alert Filter

```python
from typing import List


class EnvironmentAwareAlertFilter:
    """
    Suppresses alerts from non-production environments to prevent
    staging noise from waking on-call engineers. Production failures
    always pass through; staging failures are logged but not alerted.
    """

    def __init__(
        self,
        recorder: EnvironmentSegmentedSuccessRecorder,
        production_failure_threshold: float = 0.95,
        staging_failure_threshold: float = 0.70,
    ):
        self._recorder = recorder
        self._prod_threshold = production_failure_threshold
        self._staging_threshold = staging_failure_threshold

    def evaluate(self, tool_name: str, window_seconds: float = 1800.0) -> List[dict]:
        alerts = []
        summary = self._recorder.per_environment_summary(tool_name, window_seconds)

        prod = summary.get(Environment.PRODUCTION.value)
        if prod and prod["calls"] >= 10:
            if prod["success_rate"] < self._prod_threshold:
                alerts.append({
                    "severity": "page",
                    "tool_name": tool_name,
                    "environment": "production",
                    "success_rate": prod["success_rate"],
                    "threshold": self._prod_threshold,
                    "calls": prod["calls"],
                })

        staging = summary.get(Environment.STAGING.value)
        if staging and staging["calls"] >= 10:
            if staging["success_rate"] < self._staging_threshold:
                alerts.append({
                    "severity": "log_only",
                    "tool_name": tool_name,
                    "environment": "staging",
                    "success_rate": staging["success_rate"],
                    "threshold": self._staging_threshold,
                    "calls": staging["calls"],
                })

        return alerts
```

## Solution 5: Environment Baseline Tracker

```python
import time
from typing import Dict, Optional


class EnvironmentBaselineTracker:
    """
    Maintains rolling baseline success rates per tool per environment.
    Used to detect whether current rates represent a regression from
    the historical norm for that specific environment.
    """

    def __init__(
        self,
        recorder: EnvironmentSegmentedSuccessRecorder,
        baseline_window_seconds: float = 86400.0,
        regression_threshold_pct: float = 10.0,
    ):
        self._recorder = recorder
        self._baseline_window = baseline_window_seconds
        self._regression_threshold = regression_threshold_pct / 100.0

    def regression_report(
        self,
        tool_name: str,
        environment: Environment,
        recent_window_seconds: float = 1800.0,
    ) -> dict:
        baseline_rate = self._recorder.success_rate(
            tool_name, environment, self._baseline_window
        )
        recent_rate = self._recorder.success_rate(
            tool_name, environment, recent_window_seconds
        )
        if baseline_rate is None or recent_rate is None:
            return {
                "status": "insufficient_data",
                "tool_name": tool_name,
                "environment": environment.value,
            }
        drop = baseline_rate - recent_rate
        return {
            "status": "regression" if drop > self._regression_threshold else "normal",
            "tool_name": tool_name,
            "environment": environment.value,
            "baseline_success_rate": baseline_rate,
            "recent_success_rate": recent_rate,
            "drop": round(drop, 4),
            "threshold": self._regression_threshold,
        }
```

## Solution 6: Environment Success Rate Dashboard

```python
import time


class EnvironmentSuccessRateDashboard:
    """
    Combines per-environment summaries, divergence detection,
    alert evaluation, and regression reports into one operational view.
    """

    def __init__(
        self,
        recorder: EnvironmentSegmentedSuccessRecorder,
        divergence_detector: CrossEnvironmentDivergenceDetector,
        alert_filter: EnvironmentAwareAlertFilter,
        baseline_tracker: EnvironmentBaselineTracker,
    ):
        self._recorder = recorder
        self._divergence = divergence_detector
        self._alerts = alert_filter
        self._baseline = baseline_tracker

    def render(self, tool_names: List[str]) -> dict:
        return {
            "generated_at": time.time(),
            "per_tool_summaries": {
                tool: self._recorder.per_environment_summary(tool)
                for tool in tool_names
            },
            "diverged_tools": self._divergence.scan_all_tools(),
            "active_alerts": [
                alert
                for tool in tool_names
                for alert in self._alerts.evaluate(tool)
            ],
            "regressions": [
                self._baseline.regression_report(tool, Environment.PRODUCTION)
                for tool in tool_names
            ],
        }
```

## Comparison

| Approach | Environment Tagging | Per-Env Rates | Divergence Detection | Alert Filtering | Regression Detection |
|---|---|---|---|---|---|
| ToolCallRecord | Yes (enum) | No | No | No | No |
| EnvironmentSegmentedSuccessRecorder | Via records | Yes | No | No | No |
| CrossEnvironmentDivergenceDetector | Via recorder | No | Yes | No | No |
| EnvironmentAwareAlertFilter | Via recorder | No | No | Yes (page vs log) | No |
| EnvironmentBaselineTracker | Via recorder | No | No | No | Yes |
| EnvironmentSuccessRateDashboard | No | No | No | No | Yes |

**Best for production**: Set `AGENT_ENVIRONMENT` as a required environment variable in your deployment manifests — an agent that boots without it defaults to UNKNOWN, which surfaces immediately in dashboards as a configuration gap. Use `CrossEnvironmentDivergenceDetector` as a post-deployment gate: if production success rate drops more than 15% below staging within 30 minutes of a deployment, trigger an automatic rollback signal. Set `EnvironmentAwareAlertFilter` to `severity=page` only for production and `severity=log_only` for staging — engineers should never be paged for staging tool failures.
