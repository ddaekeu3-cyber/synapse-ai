---
title: "Agent Doesn't Implement Per-Model Latency Percentile Tracking"
description: "Agents that average latency across all LLM calls cannot detect that one model variant is consistently slower than another, or that latency has regressed after a model update. Implement per-model latency percentile tracking with sliding windows, P50/P95/P99 computation, model comparison reports, and regression detection between model versions."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-per-model-latency-percentile-tracking
tags: [latency-percentiles, per-model-metrics, p99-tracking, model-comparison, latency-regression, sliding-window]
symptoms:
  - "Latency metrics aggregated across all models — slower model hidden by faster one's average"
  - "Model upgrade causes latency regression undetected for days"
  - "No P99 tracking — only mean latency reported, masking tail behavior"
  - "Cannot compare latency between claude-opus-4-6 and claude-sonnet-4-6 in production"
  - "No alert when a model's P95 crosses the SLO threshold"
---

## Why This Happens

Mean latency is a misleading metric for LLM calls: a model with a mean of 1.5 seconds may have a P99 of 12 seconds. When multiple models are used (routing between Haiku and Opus based on task complexity, or A/B testing model versions), their latencies are often merged into a single metric. A slow Opus call dragged into the same histogram as fast Haiku calls makes both invisible. Per-model tracking requires separate sliding window histograms, independent percentile computation, and comparison tools that expose the latency difference between models.

## Solution 1: Latency Sample

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ModelLatencySample:
    model_id: str
    latency_ms: float
    input_tokens: int
    output_tokens: int
    request_type: str = ""       # e.g. "completion", "embedding", "streaming"
    feature_tag: str = ""        # caller context, e.g. "summarization"
    session_id: str = ""
    success: bool = True
    recorded_at: float = field(default_factory=time.time)

    @property
    def tokens_per_second(self) -> float:
        if self.latency_ms <= 0:
            return 0.0
        return round(self.output_tokens / (self.latency_ms / 1000.0), 2)
```

## Solution 2: Per-Model Latency Window

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class PerModelLatencyWindow:
    """
    Sliding window latency tracker for a single model.
    Computes P50, P95, P99 over configurable windows.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        self._samples: Deque[Tuple[float, float]] = deque()  # (ts, latency_ms)
        self._lock = Lock()
        self._total_samples = 0
        self._total_errors = 0

    def record(self, latency_ms: float, success: bool = True) -> None:
        now = time.time()
        with self._lock:
            self._samples.append((now, latency_ms))
            self._total_samples += 1
            if not success:
                self._total_errors += 1
            cutoff = now - self._window
            while self._samples and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def percentile(self, pct: float) -> Optional[float]:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            values = sorted(lat for ts, lat in self._samples if ts >= cutoff)
        if not values:
            return None
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def mean(self) -> Optional[float]:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            values = [lat for ts, lat in self._samples if ts >= cutoff]
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    def sample_count(self) -> int:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            return sum(1 for ts, _ in self._samples if ts >= cutoff)

    def stats(self) -> dict:
        return {
            "p50_ms": self.percentile(50),
            "p95_ms": self.percentile(95),
            "p99_ms": self.percentile(99),
            "mean_ms": self.mean(),
            "window_samples": self.sample_count(),
            "lifetime_samples": self._total_samples,
            "lifetime_errors": self._total_errors,
        }
```

## Solution 3: Per-Model Latency Registry

```python
import time
from typing import Dict, List, Optional


class PerModelLatencyRegistry:
    """
    Manages per-model latency windows and provides aggregate comparison views.
    """

    def __init__(self, window_seconds: float = 300.0):
        self._window = window_seconds
        self._windows: Dict[str, PerModelLatencyWindow] = {}

    def record(self, sample: ModelLatencySample) -> None:
        if sample.model_id not in self._windows:
            self._windows[sample.model_id] = PerModelLatencyWindow(self._window)
        self._windows[sample.model_id].record(sample.latency_ms, sample.success)

    def stats_for(self, model_id: str) -> Optional[dict]:
        w = self._windows.get(model_id)
        if not w:
            return None
        return {"model_id": model_id, **w.stats()}

    def all_stats(self) -> Dict[str, dict]:
        return {
            model_id: {"model_id": model_id, **w.stats()}
            for model_id, w in self._windows.items()
        }

    def compare(self, model_a: str, model_b: str) -> dict:
        a = self._windows.get(model_a)
        b = self._windows.get(model_b)
        if not a or not b:
            return {"error": "one or both models have no data"}

        a_p95 = a.percentile(95) or 0.0
        b_p95 = b.percentile(95) or 0.0
        faster = model_a if a_p95 <= b_p95 else model_b
        slower = model_b if a_p95 <= b_p95 else model_a
        diff = abs(a_p95 - b_p95)

        return {
            "model_a": model_a,
            "model_b": model_b,
            "a_p95_ms": a_p95,
            "b_p95_ms": b_p95,
            "p95_diff_ms": round(diff, 2),
            "faster_model": faster,
            "slower_model": slower,
            "pct_slower": round(diff / max(min(a_p95, b_p95), 1) * 100, 1),
        }

    def slowest_models(self, top_n: int = 5) -> List[dict]:
        all_stats = self.all_stats()
        return sorted(
            [s for s in all_stats.values() if s.get("p95_ms") is not None],
            key=lambda s: -(s["p95_ms"] or 0),
        )[:top_n]
```

## Solution 4: Latency SLO Policy

```python
from dataclasses import dataclass, field
from typing import Dict, Optional


@dataclass
class ModelLatencySLO:
    model_id: str
    p95_threshold_ms: float
    p99_threshold_ms: float
    min_samples: int = 20    # need at least this many samples before alerting


DEFAULT_SLOS: Dict[str, ModelLatencySLO] = {
    "claude-haiku-4-5-20251001": ModelLatencySLO(
        "claude-haiku-4-5-20251001", p95_threshold_ms=3000.0, p99_threshold_ms=6000.0
    ),
    "claude-sonnet-4-6": ModelLatencySLO(
        "claude-sonnet-4-6", p95_threshold_ms=8000.0, p99_threshold_ms=15000.0
    ),
    "claude-opus-4-6": ModelLatencySLO(
        "claude-opus-4-6", p95_threshold_ms=20000.0, p99_threshold_ms=40000.0
    ),
}
```

## Solution 5: Latency SLO Alert Manager

```python
import time
from typing import Callable, Dict, List, Optional, Set


class ModelLatencySLOAlertManager:
    """
    Checks per-model latency against SLO policies and fires alerts
    when thresholds are exceeded. Uses cooldown to prevent spam.
    """

    def __init__(
        self,
        registry: PerModelLatencyRegistry,
        slos: Dict[str, ModelLatencySLO],
        alert_fn: Callable[[dict], None],
        cooldown_s: float = 600.0,
        default_slo: Optional[ModelLatencySLO] = None,
    ):
        self._registry = registry
        self._slos = slos
        self._alert_fn = alert_fn
        self._cooldown = cooldown_s
        self._default = default_slo
        self._last_alert: Dict[str, float] = {}

    def check_all(self) -> List[dict]:
        fired = []
        for model_id, window in self._registry._windows.items():
            slo = self._slos.get(model_id, self._default)
            if not slo:
                continue
            if window.sample_count() < slo.min_samples:
                continue

            p95 = window.percentile(95)
            p99 = window.percentile(99)
            breach_level = None
            threshold = None

            if p99 and p99 > slo.p99_threshold_ms:
                breach_level = "p99"
                threshold = slo.p99_threshold_ms
                current = p99
            elif p95 and p95 > slo.p95_threshold_ms:
                breach_level = "p95"
                threshold = slo.p95_threshold_ms
                current = p95

            if breach_level is None:
                continue

            key = f"{model_id}:{breach_level}"
            last = self._last_alert.get(key, 0.0)
            if time.time() - last < self._cooldown:
                continue

            self._last_alert[key] = time.time()
            alert = {
                "event": "model_latency_slo_breach",
                "model_id": model_id,
                "breach_level": breach_level,
                "current_ms": current,
                "threshold_ms": threshold,
                "ts": time.time(),
            }
            try:
                self._alert_fn(alert)
            except Exception:
                pass
            fired.append(alert)
        return fired
```

## Solution 6: Per-Model Latency Dashboard

```python
import time


class PerModelLatencyDashboard:
    """
    Combines per-model stats, SLO compliance, and model comparison
    into a unified latency observability view.
    """

    def __init__(
        self,
        registry: PerModelLatencyRegistry,
        alert_manager: ModelLatencySLOAlertManager,
        slos: Dict[str, ModelLatencySLO],
    ):
        self._registry = registry
        self._alert_manager = alert_manager
        self._slos = slos

    def render(self) -> dict:
        all_stats = self._registry.all_stats()
        slo_status = {}
        for model_id, stats in all_stats.items():
            slo = self._slos.get(model_id)
            if slo:
                p95 = stats.get("p95_ms") or 0.0
                p99 = stats.get("p99_ms") or 0.0
                slo_status[model_id] = {
                    "p95_ms": p95,
                    "p99_ms": p99,
                    "p95_slo_ms": slo.p95_threshold_ms,
                    "p99_slo_ms": slo.p99_threshold_ms,
                    "p95_ok": p95 <= slo.p95_threshold_ms,
                    "p99_ok": p99 <= slo.p99_threshold_ms,
                    "samples": stats.get("window_samples", 0),
                }
        return {
            "generated_at": time.time(),
            "per_model_stats": all_stats,
            "slo_status": slo_status,
            "slowest_models": self._registry.slowest_models(top_n=3),
            "models_breaching_slo": [
                m for m, s in slo_status.items()
                if not s["p95_ok"] or not s["p99_ok"]
            ],
        }
```

## Comparison

| Approach | Per-Model Window | Percentile Computation | Model Comparison | SLO Alerting | Dashboard |
|---|---|---|---|---|---|
| PerModelLatencyWindow | Yes (sliding) | Yes (P50/P95/P99) | No | No | No |
| PerModelLatencyRegistry | Yes (per model) | Via windows | Yes | No | No |
| ModelLatencySLOAlertManager | Via registry | Via registry | No | Yes | No |
| PerModelLatencyDashboard | Via registry | Via registry | Via registry | Via manager | Yes |

**Best for production**: Track latency by full model ID string (including version) rather than family — `claude-sonnet-4-6` and a future `claude-sonnet-4-7` will have different latency profiles and must be tracked independently. Use a 5-minute sliding window for real-time alerting and a 24-hour window for trend analysis — the 5-minute window catches incidents, the 24-hour window catches gradual regressions. Alert at P95 for user-facing features and P99 for SLAs — the P99 is what your worst-affected users experience. Always track `tokens_per_second` alongside absolute latency: a model that is slower in wall-clock time but faster per output token may be the right trade-off for long-generation tasks.
