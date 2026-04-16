---
title: "Agent Doesn't Implement LLM Provider Latency Percentile Tracking"
description: "Agents that measure only average LLM response latency miss the tail: a provider with a 500ms mean and a 15-second P99 imposes 15-second waits on 1 in 100 requests. Implement per-provider LLM latency percentile tracking that records every response latency, computes P50/P95/P99, and alerts when tail latency exceeds SLO thresholds."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-llm-provider-latency-percentile-tracking
tags: [llm-latency, percentile-tracking, p99, provider-slo, tail-latency, performance-monitoring]
symptoms:
  - "Average LLM latency looks healthy at 800ms while P99 is 18 seconds"
  - "No per-provider breakdown — all models aggregated into one latency metric"
  - "SLO violations missed because only mean latency is tracked"
  - "Cannot detect that one model variant has degraded P95 after a provider update"
  - "On-call engineers receive user complaints about slow responses before any metric fires"
---

## Why This Happens

Mean latency hides the distribution. An LLM provider that responds in 400ms 99% of the time but 20 seconds 1% of the time has a mean near 600ms — which looks acceptable — but users experiencing the P99 tail see broken-seeming behavior. Percentile tracking requires storing individual latency observations in a sorted structure or histogram, computing percentiles on demand, and comparing them against per-provider SLOs. Without this, tail latency regressions are invisible until they generate user complaints.

## Solution 1: LLM Latency Observation

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LLMLatencyObservation:
    provider: str
    model: str
    latency_ms: float
    prompt_tokens: int
    completion_tokens: int
    timestamp: float = field(default_factory=time.time)
    request_id: str = ""
    error: bool = False
    error_type: str = ""

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def ms_per_token(self) -> float:
        return round(self.latency_ms / max(self.total_tokens, 1), 3)
```

## Solution 2: Latency Percentile Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, List, Optional, Tuple


class LLMLatencyPercentileTracker:
    """
    Tracks LLM response latency observations per (provider, model) pair.
    Supports percentile queries over a sliding time window.
    """

    def __init__(self, max_observations: int = 10000, window_seconds: float = 3600.0):
        self._max = max_observations
        self._window = window_seconds
        self._observations: Deque[LLMLatencyObservation] = deque()
        self._lock = Lock()

    def record(self, obs: LLMLatencyObservation) -> None:
        with self._lock:
            self._observations.append(obs)
            if len(self._observations) > self._max:
                self._observations.popleft()

    def _recent(
        self,
        window_seconds: Optional[float] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        exclude_errors: bool = True,
    ) -> List[LLMLatencyObservation]:
        w = window_seconds or self._window
        cutoff = time.time() - w
        with self._lock:
            result = [
                o for o in self._observations
                if o.timestamp >= cutoff
                and (provider is None or o.provider == provider)
                and (model is None or o.model == model)
                and (not exclude_errors or not o.error)
            ]
        return result

    def percentile(
        self,
        pct: float,
        window_seconds: Optional[float] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[float]:
        observations = self._recent(window_seconds, provider, model)
        if not observations:
            return None
        values = sorted(o.latency_ms for o in observations)
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def summary(
        self,
        window_seconds: Optional[float] = None,
        provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict:
        observations = self._recent(window_seconds, provider, model)
        if not observations:
            return {"observations": 0}

        latencies = [o.latency_ms for o in observations]
        errors = self._recent(window_seconds, provider, model, exclude_errors=False)
        error_count = sum(1 for o in errors if o.error)

        w = window_seconds or self._window
        return {
            "provider": provider or "all",
            "model": model or "all",
            "window_seconds": w,
            "observations": len(observations),
            "error_count": error_count,
            "mean_ms": round(sum(latencies) / len(latencies), 2),
            "p50_ms": self.percentile(50, w, provider, model),
            "p95_ms": self.percentile(95, w, provider, model),
            "p99_ms": self.percentile(99, w, provider, model),
            "max_ms": max(latencies),
            "min_ms": min(latencies),
        }

    def all_providers(self) -> List[str]:
        with self._lock:
            return list({o.provider for o in self._observations})

    def all_models(self, provider: Optional[str] = None) -> List[str]:
        with self._lock:
            return list({
                o.model for o in self._observations
                if provider is None or o.provider == provider
            })
```

## Solution 3: Provider SLO Monitor

```python
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class ProviderSLO:
    provider: str
    model: str
    p50_target_ms: float = 1000.0
    p95_target_ms: float = 5000.0
    p99_target_ms: float = 15000.0
    error_rate_target: float = 0.02   # 2%


class ProviderSLOMonitor:
    """
    Checks actual latency percentiles against declared SLOs per provider/model.
    """

    def __init__(
        self,
        tracker: LLMLatencyPercentileTracker,
        slos: List[ProviderSLO],
        window_seconds: float = 3600.0,
    ):
        self._tracker = tracker
        self._slos: Dict[tuple, ProviderSLO] = {(s.provider, s.model): s for s in slos}
        self._window = window_seconds

    def check(self, provider: str, model: str) -> dict:
        slo = self._slos.get((provider, model))
        if not slo:
            return {"provider": provider, "model": model, "status": "no_slo_defined"}

        summary = self._tracker.summary(self._window, provider, model)
        if summary.get("observations", 0) < 10:
            return {"provider": provider, "model": model, "status": "insufficient_data"}

        violations = []
        for pct_name, target in [("p50", slo.p50_target_ms), ("p95", slo.p95_target_ms), ("p99", slo.p99_target_ms)]:
            actual = summary.get(f"{pct_name}_ms")
            if actual and actual > target:
                violations.append({
                    "percentile": pct_name,
                    "target_ms": target,
                    "actual_ms": actual,
                    "excess_ms": round(actual - target, 2),
                })

        return {
            "provider": provider,
            "model": model,
            "status": "violated" if violations else "ok",
            "violations": violations,
            "summary": summary,
        }

    def check_all(self) -> List[dict]:
        return [self.check(p, m) for p, m in self._slos.keys()]

    def violated_slos(self) -> List[dict]:
        return [r for r in self.check_all() if r.get("status") == "violated"]
```

## Solution 4: LLM Call Latency Instrumentor

```python
import time
from typing import Any, Callable, Optional


class LLMCallLatencyInstrumentor:
    """
    Wraps LLM API calls and records latency observations automatically.
    """

    def __init__(
        self,
        tracker: LLMLatencyPercentileTracker,
        slo_monitor: Optional[ProviderSLOMonitor] = None,
        alert_fn: Optional[Callable[[list], None]] = None,
        alert_check_every_n: int = 50,
    ):
        self._tracker = tracker
        self._slo_monitor = slo_monitor
        self._alert_fn = alert_fn
        self._alert_every = alert_check_every_n
        self._call_count = 0

    async def call(
        self,
        llm_fn: Callable,
        provider: str,
        model: str,
        prompt_tokens: int = 0,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        start = time.time()
        error = False
        error_type = ""
        try:
            result = await llm_fn(*args, **kwargs)
            return result
        except Exception as exc:
            error = True
            error_type = type(exc).__name__
            raise
        finally:
            latency_ms = round((time.time() - start) * 1000, 2)
            completion_tokens = getattr(result if not error else None, "usage", {}).get("completion_tokens", 0) if not error else 0
            obs = LLMLatencyObservation(
                provider=provider,
                model=model,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                error=error,
                error_type=error_type,
            )
            self._tracker.record(obs)
            self._call_count += 1
            if self._slo_monitor and self._alert_fn and self._call_count % self._alert_every == 0:
                violated = self._slo_monitor.violated_slos()
                if violated:
                    self._alert_fn(violated)
```

## Solution 5: Latency Regression Detector

```python
from typing import Optional


class LLMLatencyRegressionDetector:
    """
    Compares recent P95 against a baseline window to detect regressions.
    """

    def __init__(
        self,
        tracker: LLMLatencyPercentileTracker,
        regression_threshold_pct: float = 30.0,
        baseline_window_seconds: float = 86400.0,
        recent_window_seconds: float = 3600.0,
    ):
        self._tracker = tracker
        self._threshold = regression_threshold_pct / 100.0
        self._baseline_window = baseline_window_seconds
        self._recent_window = recent_window_seconds

    def check(self, provider: str, model: str) -> dict:
        baseline_p95 = self._tracker.percentile(95, self._baseline_window, provider, model)
        recent_p95 = self._tracker.percentile(95, self._recent_window, provider, model)

        if baseline_p95 is None or recent_p95 is None:
            return {"status": "insufficient_data", "provider": provider, "model": model}

        change = (recent_p95 - baseline_p95) / max(baseline_p95, 1)
        regressed = change > self._threshold

        return {
            "status": "regressed" if regressed else "ok",
            "provider": provider,
            "model": model,
            "baseline_p95_ms": baseline_p95,
            "recent_p95_ms": recent_p95,
            "change_pct": round(change * 100, 1),
        }
```

## Solution 6: LLM Provider Latency Dashboard

```python
import time


class LLMProviderLatencyDashboard:
    """
    Renders per-provider, per-model latency summaries and SLO status.
    """

    def __init__(
        self,
        tracker: LLMLatencyPercentileTracker,
        slo_monitor: ProviderSLOMonitor,
        regression_detector: LLMLatencyRegressionDetector,
    ):
        self._tracker = tracker
        self._slo = slo_monitor
        self._regression = regression_detector

    def render(self) -> dict:
        providers = self._tracker.all_providers()
        per_provider = {}
        for provider in providers:
            models = self._tracker.all_models(provider)
            per_provider[provider] = {
                model: {
                    "summary": self._tracker.summary(provider=provider, model=model),
                    "slo": self._slo.check(provider, model),
                    "regression": self._regression.check(provider, model),
                }
                for model in models
            }

        return {
            "generated_at": time.time(),
            "providers": per_provider,
            "violated_slos": self._slo.violated_slos(),
        }
```

## Comparison

| Approach | Per-Provider Tracking | Percentile Queries | SLO Check | Regression Detection | Dashboard |
|---|---|---|---|---|---|
| LLMLatencyPercentileTracker | Yes | Yes (P50/P95/P99) | No | No | No |
| ProviderSLOMonitor | Via tracker | Via tracker | Yes | No | No |
| LLMCallLatencyInstrumentor | Via tracker | No | Via SLO monitor | No | No |
| LLMLatencyRegressionDetector | Via tracker | Via tracker | No | Yes | No |
| LLMProviderLatencyDashboard | No | No | No | No | Yes |

**Best for production**: Track latency separately for each `(provider, model)` pair — a provider with two models often has very different latency profiles, and aggregating them obscures regressions in one. Set P99 SLO targets conservatively (e.g., 15s for GPT-4, 5s for GPT-3.5) and P95 targets at 50% of P99 — this catches tail degradation before it reaches the extreme tail. Alert when `regression_detector.check()` shows >30% P95 increase in the last hour vs. the 24-hour baseline: this is the earliest signal of a provider degradation. Use `window_seconds=300` for real-time alerting and `window_seconds=3600` for trend reporting.
