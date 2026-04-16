---
title: "Agent Doesn't Implement Model Version Drift Detection"
description: "Agents that pin to a model alias like 'claude-sonnet-latest' instead of a specific version ID receive silent model updates that change output behavior without any code change — response formatting shifts, tool call JSON changes structure, reasoning patterns change. Implement model version drift detection that records the exact model version used per call and alerts when the version changes unexpectedly."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-model-version-drift-detection
tags: [model-version, drift-detection, alias-resolution, version-pinning, behavioral-change, model-monitoring]
symptoms:
  - "Agent behavior changes silently after a provider updates the model behind an alias"
  - "No record of which exact model version handled each request"
  - "Output format regressions after an alias update are mistakenly attributed to prompt changes"
  - "No alert when the resolved model version changes between requests"
  - "Cannot reproduce a past response because the exact model version is unknown"
---

## Why This Happens

LLM providers use aliases (`claude-sonnet-latest`, `gpt-4o`) that resolve to a specific model version behind the scenes. When the provider updates the alias, calls using the alias silently switch to a new model. The response object from the API typically includes the resolved model ID — but if the agent does not record it, version changes are invisible. Model version drift detection requires recording the resolved version on every call, comparing it to the last known version, and emitting a drift event when it changes.

## Solution 1: Model Version Record

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ModelVersionRecord:
    alias: str                        # what was requested: "claude-sonnet-latest"
    resolved_version: str             # what was actually used: "claude-sonnet-4-6-20251022"
    first_seen_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    call_count: int = 0

    def matches(self, resolved: str) -> bool:
        return self.resolved_version == resolved

    def update(self) -> None:
        self.last_seen_at = time.time()
        self.call_count += 1
```

## Solution 2: Model Version Tracker

```python
import time
from threading import Lock
from typing import Dict, List, Optional


class ModelVersionTracker:
    """
    Records resolved model versions per alias and detects
    when the version changes — indicating a silent model update.
    """

    def __init__(self):
        self._current: Dict[str, ModelVersionRecord] = {}
        self._history: Dict[str, List[ModelVersionRecord]] = {}
        self._drift_events: List[dict] = []
        self._lock = Lock()

    def observe(self, alias: str, resolved_version: str) -> Optional[dict]:
        """
        Records an observation. Returns a drift event dict if the version changed,
        None if this is the first observation or same as current.
        """
        with self._lock:
            current = self._current.get(alias)

            if current is None:
                # First time seeing this alias
                rec = ModelVersionRecord(alias=alias, resolved_version=resolved_version)
                self._current[alias] = rec
                self._history.setdefault(alias, []).append(rec)
                return None

            if current.matches(resolved_version):
                current.update()
                return None

            # Version changed — drift detected
            old_version = current.resolved_version
            new_rec = ModelVersionRecord(alias=alias, resolved_version=resolved_version)
            self._current[alias] = new_rec
            self._history.setdefault(alias, []).append(new_rec)

            event = {
                "type": "model_version_drift",
                "alias": alias,
                "old_version": old_version,
                "new_version": resolved_version,
                "detected_at": time.time(),
                "previous_call_count": current.call_count,
            }
            self._drift_events.append(event)
            return event

    def current_versions(self) -> Dict[str, str]:
        with self._lock:
            return {alias: rec.resolved_version for alias, rec in self._current.items()}

    def drift_events(self, window_seconds: float = 86400.0) -> List[dict]:
        cutoff = time.time() - window_seconds
        with self._lock:
            return [e for e in self._drift_events if e["detected_at"] >= cutoff]

    def version_history(self, alias: str) -> List[dict]:
        with self._lock:
            return [
                {
                    "version": r.resolved_version,
                    "first_seen": r.first_seen_at,
                    "last_seen": r.last_seen_at,
                    "calls": r.call_count,
                }
                for r in self._history.get(alias, [])
            ]
```

## Solution 3: Version-Aware LLM Call Interceptor

```python
import time
from typing import Any, Callable, Dict, List, Optional


class VersionAwareLLMCallInterceptor:
    """
    Wraps LLM calls to extract and record the resolved model version
    from the response. Works with any client that returns a response
    object with a 'model' attribute.
    """

    def __init__(
        self,
        tracker: ModelVersionTracker,
        alias: str,
    ):
        self._tracker = tracker
        self._alias = alias

    async def call(
        self,
        llm_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        response = await llm_fn(*args, **kwargs)
        resolved = self._extract_version(response)
        if resolved:
            drift_event = self._tracker.observe(self._alias, resolved)
            if drift_event:
                self._on_drift(drift_event)
        return response

    def _extract_version(self, response: Any) -> Optional[str]:
        # Standard across Anthropic and OpenAI response objects
        if hasattr(response, "model"):
            return response.model
        if isinstance(response, dict):
            return response.get("model")
        return None

    def _on_drift(self, event: dict) -> None:
        # Override or inject alerting logic here
        pass
```

## Solution 4: Drift Alert Manager

```python
import time
from typing import Callable, List, Optional


class ModelDriftAlertManager:
    """
    Fires alert callbacks when model version drift is detected.
    Supports cooldown to avoid alert storms during a rollout.
    """

    def __init__(
        self,
        tracker: ModelVersionTracker,
        alert_fn: Optional[Callable[[dict], None]] = None,
        cooldown_seconds: float = 300.0,
    ):
        self._tracker = tracker
        self._alert_fn = alert_fn
        self._cooldown = cooldown_seconds
        self._last_alert: dict = {}   # alias -> timestamp

    def check_and_alert(self) -> List[dict]:
        recent_drifts = self._tracker.drift_events(window_seconds=self._cooldown)
        fired = []
        for event in recent_drifts:
            alias = event["alias"]
            last = self._last_alert.get(alias, 0)
            if time.time() - last < self._cooldown:
                continue
            self._last_alert[alias] = time.time()
            if self._alert_fn:
                try:
                    self._alert_fn(event)
                except Exception:
                    pass
            fired.append(event)
        return fired
```

## Solution 5: Version Pin Validator

```python
from typing import Dict, Optional


class ModelVersionPinValidator:
    """
    Validates that observed resolved versions match expected pinned versions.
    Use this to detect when an explicitly pinned model ID is somehow
    resolved to a different version (provider-side experiment or misconfiguration).
    """

    def __init__(self, expected_pins: Dict[str, str]):
        self._pins = expected_pins   # alias -> expected_resolved_version

    def validate(self, alias: str, resolved_version: str) -> Optional[dict]:
        expected = self._pins.get(alias)
        if expected is None:
            return None   # no pin configured — skip
        if resolved_version != expected:
            return {
                "violation": True,
                "alias": alias,
                "expected_version": expected,
                "actual_version": resolved_version,
                "severity": "high",
            }
        return None

    def validate_all(self, current_versions: Dict[str, str]) -> list:
        violations = []
        for alias, resolved in current_versions.items():
            result = self.validate(alias, resolved)
            if result:
                violations.append(result)
        return violations
```

## Solution 6: Model Version Drift Dashboard

```python
import time


class ModelVersionDriftDashboard:
    """
    Combines version history, drift events, and pin violations
    into a single operational snapshot.
    """

    def __init__(
        self,
        tracker: ModelVersionTracker,
        pin_validator: Optional[ModelVersionPinValidator] = None,
    ):
        self._tracker = tracker
        self._validator = pin_validator

    def render(self) -> dict:
        current = self._tracker.current_versions()
        recent_drifts = self._tracker.drift_events(window_seconds=86400.0)
        pin_violations = (
            self._validator.validate_all(current) if self._validator else []
        )

        return {
            "generated_at": time.time(),
            "current_versions": current,
            "drift_events_24h": len(recent_drifts),
            "recent_drifts": recent_drifts[-10:],
            "pin_violations": pin_violations,
            "status": (
                "VIOLATION" if pin_violations
                else "DRIFTED" if recent_drifts
                else "stable"
            ),
        }
```

## Comparison

| Approach | Version Recording | Drift Detection | Alias History | Pin Validation | Alert Firing |
|---|---|---|---|---|---|
| ModelVersionTracker | Yes | Yes (on change) | Yes | No | No |
| VersionAwareLLMCallInterceptor | Via tracker | Via tracker | No | No | No |
| ModelDriftAlertManager | Via tracker | Via tracker | No | No | Yes (cooldown) |
| ModelVersionPinValidator | No | No | No | Yes | No |
| ModelVersionDriftDashboard | No | No | Via tracker | Via validator | No |

**Best for production**: Record `response.model` on every LLM API call — it costs nothing and provides the exact version for reproducibility and audit. Use `ModelVersionPinValidator` for production deployments: explicitly pin the full model ID (e.g., `claude-sonnet-4-6-20251022`) rather than an alias, and alert if the resolved version ever deviates from the pin. Set `cooldown_seconds=300` in `ModelDriftAlertManager` to avoid flooding on-call channels during a gradual rollout. When drift is detected, run a regression test suite against the new version before accepting it — behavioral changes in model updates often affect edge cases not covered by happy-path evals.
