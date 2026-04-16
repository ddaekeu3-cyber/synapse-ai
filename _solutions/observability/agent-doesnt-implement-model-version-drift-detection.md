---
title: "Agent Doesn't Implement Model Version Drift Detection"
description: "Agents that don't track which model version produced each response cannot detect when a provider silently updates the underlying model — response quality changes, tool call format shifts, and cost-per-token varies, all without any deployment event on the agent side. Implement model version drift detection that records the exact model version from each API response, computes behavioral fingerprints, and alerts when response characteristics shift beyond baseline variance."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-model-version-drift-detection
tags: [model-drift, model-versioning, behavioral-drift, provider-updates, response-fingerprint, model-observability]
symptoms:
  - "Response quality degrades after a provider maintenance window with no change on our side"
  - "Tool call format silently changes — JSON keys shift from snake_case to camelCase"
  - "No record of which model version produced a given response — cannot correlate regressions"
  - "Cost-per-token changes between weeks but no alert fires because no version tracking exists"
  - "Cannot distinguish 'our prompt changed' from 'provider updated the model'"
---

## Why This Happens

LLM providers update models continuously — safety patches, capability improvements, inference optimizations. Some providers distinguish versions by date suffix (`claude-3-5-sonnet-20241022`); others may silently update the model behind the same API name. Without recording the model version string returned in each response, regression analysis cannot isolate provider changes from agent code changes. Behavioral fingerprinting adds a second signal: measurable response characteristics (average length, tool call rate, refusal rate) that shift when the underlying model changes.

## Solution 1: Model Version Record

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ModelVersionRecord:
    model_requested: str          # what the agent sent in the request
    model_served: str             # what the provider reports in the response
    session_id: Optional[str]
    prompt_tokens: int
    completion_tokens: int
    response_length_chars: int
    tool_calls_made: int
    finish_reason: str            # "stop" | "length" | "tool_use" | "content_filter"
    response_time_ms: float
    recorded_at: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def version_changed(self) -> bool:
        return self.model_requested != self.model_served

    def cost_per_1k_tokens(self, cost_map: Dict[str, float]) -> Optional[float]:
        rate = cost_map.get(self.model_served)
        if rate is None:
            return None
        total = self.prompt_tokens + self.completion_tokens
        return round(rate * total / 1000, 6)
```

## Solution 2: Model Version Extractor

```python
from typing import Any, Dict, Optional


class ModelVersionExtractor:
    """
    Extracts the actual model version from provider API responses.
    Different providers expose this in different response fields.
    """

    def extract_anthropic(self, response: Dict[str, Any]) -> str:
        return response.get("model", "unknown")

    def extract_openai(self, response: Dict[str, Any]) -> str:
        return response.get("model", "unknown")

    def extract_from_response(
        self,
        response: Dict[str, Any],
        provider: str,
        requested_model: str,
    ) -> str:
        if provider == "anthropic":
            return self.extract_anthropic(response)
        if provider in ("openai", "azure_openai"):
            return self.extract_openai(response)
        # Generic fallback
        for field in ("model", "model_version", "engine", "deployment_name"):
            if field in response:
                return str(response[field])
        return requested_model   # assume no drift if field not found

    def build_record(
        self,
        response: Dict[str, Any],
        provider: str,
        requested_model: str,
        session_id: Optional[str],
        response_time_ms: float,
    ) -> ModelVersionRecord:
        served = self.extract_from_response(response, provider, requested_model)
        usage = response.get("usage", {})
        content = response.get("content", "") or ""
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") for c in content if isinstance(c, dict)
            )

        tool_calls = 0
        if isinstance(response.get("content"), list):
            tool_calls = sum(
                1 for c in response["content"]
                if isinstance(c, dict) and c.get("type") == "tool_use"
            )

        return ModelVersionRecord(
            model_requested=requested_model,
            model_served=served,
            session_id=session_id,
            prompt_tokens=usage.get("input_tokens", usage.get("prompt_tokens", 0)),
            completion_tokens=usage.get("output_tokens", usage.get("completion_tokens", 0)),
            response_length_chars=len(str(content)),
            tool_calls_made=tool_calls,
            finish_reason=response.get("stop_reason", response.get("finish_reason", "unknown")),
            response_time_ms=response_time_ms,
        )
```

## Solution 3: Behavioral Fingerprint Computer

```python
import math
from typing import List, Optional


@dataclass
class BehavioralFingerprint:
    model_version: str
    sample_count: int
    avg_response_length: float
    avg_completion_tokens: float
    tool_call_rate: float          # fraction of responses with tool calls
    length_finish_rate: float      # fraction truncated by length
    avg_response_time_ms: float
    computed_at: float = field(default_factory=time.time)

    def divergence_from(self, other: "BehavioralFingerprint") -> Dict[str, float]:
        """Returns per-metric percent change from other to self."""
        def pct_change(a: float, b: float) -> float:
            if b == 0:
                return 0.0
            return round(abs(a - b) / b * 100, 2)

        return {
            "response_length_pct": pct_change(self.avg_response_length, other.avg_response_length),
            "completion_tokens_pct": pct_change(self.avg_completion_tokens, other.avg_completion_tokens),
            "tool_call_rate_pct": pct_change(self.tool_call_rate, other.tool_call_rate),
            "response_time_pct": pct_change(self.avg_response_time_ms, other.avg_response_time_ms),
        }


class BehavioralFingerprintComputer:
    """
    Computes a behavioral fingerprint from a sample of ModelVersionRecords.
    Used to compare behavior before and after a suspected model update.
    """

    def compute(
        self,
        records: List[ModelVersionRecord],
        model_version: str,
    ) -> Optional[BehavioralFingerprint]:
        if len(records) < 10:
            return None

        n = len(records)
        return BehavioralFingerprint(
            model_version=model_version,
            sample_count=n,
            avg_response_length=round(sum(r.response_length_chars for r in records) / n, 1),
            avg_completion_tokens=round(sum(r.completion_tokens for r in records) / n, 1),
            tool_call_rate=round(sum(1 for r in records if r.tool_calls_made > 0) / n, 4),
            length_finish_rate=round(
                sum(1 for r in records if r.finish_reason == "length") / n, 4
            ),
            avg_response_time_ms=round(sum(r.response_time_ms for r in records) / n, 2),
        )
```

## Solution 4: Model Version Drift Tracker

```python
from collections import defaultdict, deque
from typing import Dict, List, Optional


class ModelVersionDriftTracker:
    """
    Maintains rolling histories of ModelVersionRecords per model.
    Detects version changes and computes behavioral divergence.
    """

    def __init__(
        self,
        window_size: int = 200,
        fingerprinter: Optional[BehavioralFingerprintComputer] = None,
    ) -> None:
        self._window = window_size
        self._fingerprinter = fingerprinter or BehavioralFingerprintComputer()
        self._records: Dict[str, deque] = defaultdict(lambda: deque(maxlen=window_size))
        self._version_history: Dict[str, List[str]] = defaultdict(list)

    def record(self, rec: ModelVersionRecord) -> None:
        key = rec.model_requested
        self._records[key].append(rec)

        history = self._version_history[key]
        if not history or history[-1] != rec.model_served:
            history.append(rec.model_served)

    def version_changes(self, requested_model: str) -> List[str]:
        return list(self._version_history.get(requested_model, []))

    def current_fingerprint(self, requested_model: str) -> Optional[BehavioralFingerprint]:
        records = list(self._records.get(requested_model, []))
        if not records:
            return None
        current_version = records[-1].model_served
        recent = [r for r in records if r.model_served == current_version]
        return self._fingerprinter.compute(recent, current_version)

    def baseline_fingerprint(self, requested_model: str) -> Optional[BehavioralFingerprint]:
        records = list(self._records.get(requested_model, []))
        if len(records) < 20:
            return None
        versions = self._version_history.get(requested_model, [])
        if len(versions) < 2:
            # No version change detected — use oldest 50% as baseline
            baseline_records = records[:len(records) // 2]
            if not baseline_records:
                return None
            return self._fingerprinter.compute(baseline_records, baseline_records[0].model_served)
        # Use records from before the most recent version change as baseline
        previous_version = versions[-2]
        baseline_records = [r for r in records if r.model_served == previous_version]
        return self._fingerprinter.compute(baseline_records, previous_version)
```

## Solution 5: Model Drift Alert Manager

```python
import time
from typing import List, Optional


class ModelDriftAlertManager:
    """
    Fires alerts when behavioral divergence exceeds thresholds
    or when an unexpected model version is detected.
    """

    def __init__(
        self,
        tracker: ModelVersionDriftTracker,
        divergence_warning_pct: float = 20.0,
        divergence_critical_pct: float = 50.0,
        cooldown_seconds: float = 3600.0,
    ) -> None:
        self._tracker = tracker
        self._warning = divergence_warning_pct
        self._critical = divergence_critical_pct
        self._cooldown = cooldown_seconds
        self._last_fired: Dict[str, float] = {}

    def _can_fire(self, key: str) -> bool:
        last = self._last_fired.get(key, 0.0)
        if time.time() - last >= self._cooldown:
            self._last_fired[key] = time.time()
            return True
        return False

    def check(self, requested_model: str) -> List[dict]:
        alerts = []
        versions = self._tracker.version_changes(requested_model)

        if len(versions) > 1 and self._can_fire(f"{requested_model}:version_change"):
            alerts.append({
                "type": "model_version_change",
                "model": requested_model,
                "previous_version": versions[-2],
                "current_version": versions[-1],
                "severity": "warning",
                "message": (
                    f"Model '{requested_model}' switched from "
                    f"'{versions[-2]}' to '{versions[-1]}'"
                ),
            })

        current = self._tracker.current_fingerprint(requested_model)
        baseline = self._tracker.baseline_fingerprint(requested_model)

        if current and baseline:
            divergence = current.divergence_from(baseline)
            max_div = max(divergence.values(), default=0.0)

            if max_div >= self._critical and self._can_fire(f"{requested_model}:critical"):
                alerts.append({
                    "type": "behavioral_drift_critical",
                    "model": requested_model,
                    "max_divergence_pct": max_div,
                    "divergence": divergence,
                    "severity": "critical",
                    "message": f"Model '{requested_model}' behavior changed by {max_div:.1f}% — investigate",
                })
            elif max_div >= self._warning and self._can_fire(f"{requested_model}:warning"):
                alerts.append({
                    "type": "behavioral_drift_warning",
                    "model": requested_model,
                    "max_divergence_pct": max_div,
                    "divergence": divergence,
                    "severity": "warning",
                })

        return alerts
```

## Solution 6: Model Version Drift Dashboard

```python
import time


class ModelVersionDriftDashboard:
    """
    Combines version history, behavioral fingerprints, and drift alerts
    into a single model observability report.
    """

    def __init__(
        self,
        tracker: ModelVersionDriftTracker,
        alert_manager: ModelDriftAlertManager,
        monitored_models: List[str],
    ) -> None:
        self._tracker = tracker
        self._alerts = alert_manager
        self._models = monitored_models

    def render(self) -> dict:
        model_reports = {}
        all_alerts = []

        for model in self._models:
            versions = self._tracker.version_changes(model)
            current_fp = self._tracker.current_fingerprint(model)
            baseline_fp = self._tracker.baseline_fingerprint(model)
            model_alerts = self._alerts.check(model)
            all_alerts.extend(model_alerts)

            model_reports[model] = {
                "version_history": versions,
                "current_version": versions[-1] if versions else None,
                "version_changes": max(0, len(versions) - 1),
                "current_fingerprint": {
                    "avg_response_length": current_fp.avg_response_length if current_fp else None,
                    "tool_call_rate": current_fp.tool_call_rate if current_fp else None,
                    "avg_completion_tokens": current_fp.avg_completion_tokens if current_fp else None,
                } if current_fp else None,
                "divergence_from_baseline": (
                    current_fp.divergence_from(baseline_fp)
                    if current_fp and baseline_fp else None
                ),
                "alerts": model_alerts,
            }

        return {
            "generated_at": time.time(),
            "models": model_reports,
            "total_alerts": len(all_alerts),
            "critical_alerts": sum(1 for a in all_alerts if a.get("severity") == "critical"),
        }
```

## Comparison

| Approach | Version Recording | Behavioral Fingerprint | Drift Detection | Version Change Alert | Dashboard |
|---|---|---|---|---|---|
| ModelVersionExtractor | Yes (per response) | No | No | No | No |
| BehavioralFingerprintComputer | No | Yes (5 metrics) | No | No | No |
| ModelVersionDriftTracker | Via extractor | Via fingerprinter | Yes | No | No |
| ModelDriftAlertManager | No | No | Via tracker | Yes | No |
| ModelVersionDriftDashboard | No | No | No | Via manager | Yes |

**Best for production**: Record `model_served` from every API response — providers like Anthropic include it in the response body. Build a behavioral fingerprint after 50+ samples to ensure statistical stability before enabling drift alerts. Set `divergence_warning_pct=20` for response length (normal variance) but `divergence_warning_pct=10` for tool call rate (format changes are more dangerous). Correlate version change events with deployment events in your change management system — if a version change coincides with a deployment, the behavioral shift may be from your prompt, not the model.
