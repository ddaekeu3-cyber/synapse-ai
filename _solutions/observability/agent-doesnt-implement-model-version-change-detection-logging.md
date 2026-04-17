---
title: "Agent Doesn't Implement Model Version Change Detection Logging"
description: "Agents that do not track which model version produced each response cannot correlate quality regressions, latency changes, or behavior shifts with model updates: a provider silently upgrades the model behind an alias ('claude-3-5-sonnet-latest'), and suddenly response style changes, but there is no log event connecting the change to its cause. Implement model version change detection that records the model version on every response, detects when the version changes between requests, and emits a structured change event for immediate investigation."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-model-version-change-detection-logging
tags: [model-versioning, version-detection, model-drift, alias-resolution, deployment-tracking, llm-observability]
symptoms:
  - "A provider updates the model behind 'gpt-4o-latest' and response quality changes — no one knows why"
  - "Model version is not recorded in any log — cannot determine which model version produced a given response"
  - "Quality regressions after a provider update are discovered through user complaints, not metrics"
  - "No alert fires when the resolved model version changes between two consecutive requests"
  - "Cannot reconstruct which model version was active during a specific time window for incident analysis"
---

## Why This Happens

LLM providers use aliases (model names that resolve to a specific underlying version) and rolling updates (the alias resolves to a new version without any notification to the caller). When a caller uses `claude-sonnet-latest` or `gpt-4o`, the actual model version servicing the request can change at any time. Most agent implementations log the model name they requested, not the model version that actually responded. The actual version is typically available in the API response headers or response body (`model` field in OpenAI-compatible APIs). Recording and comparing this value across requests enables version change detection.

## Solution 1: Model Version Record

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ModelVersionRecord:
    requested_model: str            # what the caller asked for (alias or version)
    resolved_model: str             # what the API actually used (from response)
    provider: str                   # "anthropic" | "openai" | "google" | etc.
    session_id: str = ""
    request_id: str = ""
    response_latency_ms: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    recorded_at: float = field(default_factory=time.time)

    @property
    def is_alias(self) -> bool:
        """True if the resolved model differs from what was requested."""
        return self.requested_model != self.resolved_model

    @property
    def version_key(self) -> str:
        return f"{self.provider}:{self.resolved_model}"
```

## Solution 2: Model Version Extractor

```python
import re
from typing import Any, Dict, Optional


class ModelVersionExtractor:
    """
    Extracts the resolved model version from an LLM API response.
    Handles OpenAI-compatible and Anthropic response formats.
    """

    def extract_from_openai_response(self, response: Any) -> Optional[str]:
        if hasattr(response, "model"):
            return response.model
        if isinstance(response, dict):
            return response.get("model")
        return None

    def extract_from_anthropic_response(self, response: Any) -> Optional[str]:
        if hasattr(response, "model"):
            return response.model
        if isinstance(response, dict):
            return response.get("model")
        return None

    def extract_from_headers(self, headers: Dict[str, str]) -> Optional[str]:
        for key in ("x-model-id", "x-model", "model-id", "x-served-by"):
            value = headers.get(key) or headers.get(key.title())
            if value:
                return value
        return None

    def extract(self, response: Any, headers: Dict[str, str] = None) -> Optional[str]:
        version = self.extract_from_openai_response(response)
        if version:
            return version
        if headers:
            return self.extract_from_headers(headers)
        return None
```

## Solution 3: Model Version Tracker

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class ModelVersionChangeEvent:
    def __init__(
        self,
        requested_model: str,
        previous_version: str,
        new_version: str,
        provider: str,
        detected_at: float = None,
    ):
        self.requested_model = requested_model
        self.previous_version = previous_version
        self.new_version = new_version
        self.provider = provider
        self.detected_at = detected_at or time.time()

    def to_dict(self) -> dict:
        return {
            "event": "model_version_change",
            "requested_model": self.requested_model,
            "previous_version": self.previous_version,
            "new_version": self.new_version,
            "provider": self.provider,
            "detected_at": self.detected_at,
        }


class ModelVersionTracker:
    """
    Tracks the resolved model version per requested model alias.
    Detects and records version changes when the resolved model
    differs from the previously observed version.
    """

    def __init__(self):
        self._known_versions: Dict[str, str] = {}   # requested_model -> resolved_model
        self._change_events: List[ModelVersionChangeEvent] = []
        self._lock = Lock()
        self._record_count = 0

    def observe(self, record: ModelVersionRecord) -> Optional[ModelVersionChangeEvent]:
        """
        Returns a ModelVersionChangeEvent if the resolved version changed,
        None if the version is the same as previously observed.
        """
        key = f"{record.provider}:{record.requested_model}"
        with self._lock:
            self._record_count += 1
            previous = self._known_versions.get(key)
            self._known_versions[key] = record.resolved_model

            if previous is not None and previous != record.resolved_model:
                event = ModelVersionChangeEvent(
                    requested_model=record.requested_model,
                    previous_version=previous,
                    new_version=record.resolved_model,
                    provider=record.provider,
                )
                self._change_events.append(event)
                return event

        return None

    def current_versions(self) -> Dict[str, str]:
        with self._lock:
            return dict(self._known_versions)

    def recent_changes(self, limit: int = 20) -> List[ModelVersionChangeEvent]:
        with self._lock:
            return list(self._change_events[-limit:])

    def total_observations(self) -> int:
        return self._record_count
```

## Solution 4: Version Change Alert Manager

```python
import time
from typing import Callable, List, Optional


class VersionChangeAlertManager:
    """
    Emits structured alerts when model version changes are detected.
    Supports cooldown to prevent alert storms during provider rolling updates.
    """

    def __init__(
        self,
        alert_fn: Optional[Callable[[dict], None]] = None,
        cooldown_seconds: float = 300.0,
    ):
        self._alert_fn = alert_fn or (lambda r: None)
        self._cooldown = cooldown_seconds
        self._last_alert: dict = {}   # requested_model -> last_alert_time
        self._alert_count = 0

    def evaluate(self, event: Optional[ModelVersionChangeEvent]) -> bool:
        if event is None:
            return False

        key = f"{event.provider}:{event.requested_model}"
        now = time.time()
        last = self._last_alert.get(key, 0)

        if now - last < self._cooldown:
            return False  # within cooldown

        self._last_alert[key] = now
        self._alert_count += 1
        self._alert_fn(event.to_dict())
        return True

    def alert_count(self) -> int:
        return self._alert_count
```

## Solution 5: Model Version Observability Logger

```python
import time
from typing import List


class ModelVersionObservabilityLogger:
    """
    Records every model version observation and version change event.
    Provides a time-series view of which model versions were active.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: List[dict] = []
        self._change_count = 0

    def record_observation(self, record: ModelVersionRecord) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": record.recorded_at,
            "type": "observation",
            "provider": record.provider,
            "requested_model": record.requested_model,
            "resolved_model": record.resolved_model,
            "is_alias": record.is_alias,
            "session_id": record.session_id,
            "latency_ms": record.response_latency_ms,
        })

    def record_change(self, event: ModelVersionChangeEvent) -> None:
        self._change_count += 1
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": event.detected_at,
            "type": "version_change",
            "provider": event.provider,
            "requested_model": event.requested_model,
            "previous_version": event.previous_version,
            "new_version": event.new_version,
        })

    def version_timeline(
        self, requested_model: str, window_seconds: float = 86400.0
    ) -> List[dict]:
        cutoff = time.time() - window_seconds
        return [
            r for r in self._records
            if r["ts"] >= cutoff and r.get("requested_model") == requested_model
        ]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        changes = [r for r in recent if r["type"] == "version_change"]
        observations = [r for r in recent if r["type"] == "observation"]

        resolved_versions: dict = {}
        for r in observations:
            rv = r.get("resolved_model", "unknown")
            resolved_versions[rv] = resolved_versions.get(rv, 0) + 1

        return {
            "window_seconds": window_seconds,
            "observations": len(observations),
            "version_changes": len(changes),
            "resolved_versions": resolved_versions,
            "total_changes_all_time": self._change_count,
        }
```

## Solution 6: Model Version Dashboard

```python
import time


class ModelVersionDashboard:
    """
    Combines version tracking, alert history, and observation log
    into a single operational view.
    """

    def __init__(
        self,
        tracker: ModelVersionTracker,
        alert_manager: VersionChangeAlertManager,
        obs_logger: ModelVersionObservabilityLogger,
    ):
        self._tracker = tracker
        self._alerts = alert_manager
        self._logger = obs_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "current_versions": self._tracker.current_versions(),
            "recent_changes": [e.to_dict() for e in self._tracker.recent_changes(10)],
            "alert_count": self._alerts.alert_count(),
            "observation_summary_24h": self._logger.summary(86400.0),
            "total_observations": self._tracker.total_observations(),
        }
```

## Comparison

| Approach | Version Extraction | Change Detection | Alert Emission | Version Timeline | Dashboard |
|---|---|---|---|---|---|
| ModelVersionExtractor | Yes (OpenAI + Anthropic) | No | No | No | No |
| ModelVersionTracker | No | Yes (per-alias) | No | No | No |
| VersionChangeAlertManager | No | No | Yes (cooldown) | No | No |
| ModelVersionObservabilityLogger | No | No | No | Yes | No |
| ModelVersionDashboard | No | No | No | No | Yes |

**Best for production**: Always log `resolved_model` from the API response, not `requested_model` — the alias you request is not the version you get. Set `cooldown_seconds=300` for version change alerts: a provider doing a rolling update may produce a mix of old and new versions for several minutes, and alerting on every flip creates noise. After a version change is detected, run a targeted quality check against the new version using a fixed evaluation set before routing production traffic — the `ModelVersionDashboard` should show the change event as a signal to initiate this check. Store the `version_timeline` for each model alias for at least 90 days: compliance frameworks often require demonstrating which model version processed which data, and this timeline is the evidence.
