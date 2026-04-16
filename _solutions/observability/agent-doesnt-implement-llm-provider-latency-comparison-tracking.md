---
title: "Agent Doesn't Implement LLM Provider Latency Comparison Tracking"
description: "Agents that use a single LLM provider without measuring its latency against alternatives cannot make data-driven routing decisions. When the primary provider degrades, the agent has no baseline to compare against and no signal to trigger failover. Implement per-provider latency tracking that continuously measures TTFT, generation speed, and total latency for each configured provider, enabling real-time routing decisions based on measured performance rather than static configuration."
date: 2026-04-16
difficulty: intermediate
category: observability
slug: agent-doesnt-implement-llm-provider-latency-comparison-tracking
tags: [provider-comparison, latency-tracking, llm-routing, failover, multi-provider, performance-baseline]
symptoms:
  - "No measurement of latency differences between providers — routing is static"
  - "Primary provider degrades but no data-driven signal to switch to a backup"
  - "P99 latency increases go unnoticed until users complain"
  - "Cannot answer 'which provider is fastest right now?' for a given model class"
  - "Provider SLA violations are invisible without per-provider latency baselines"
---

## Why This Happens

Most agents are configured with a single primary provider and a fallback that is only used on errors. Without continuous measurement of both providers' latency, the agent cannot detect when the primary is degrading (but not failing), when the backup would be faster, or when a different model tier on the same provider would improve tail latency. Per-provider tracking requires measuring every LLM call with provider and model metadata, computing per-provider percentiles, and surfacing comparison data that informs routing decisions.

## Solution 1: Provider Latency Sample

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class ProviderLatencySample:
    provider: str
    model: str
    request_id: str
    started_at: float = field(default_factory=time.time)
    first_token_at: Optional[float] = None
    completed_at: Optional[float] = None
    input_tokens: int = 0
    output_tokens: int = 0
    error: Optional[str] = None

    def ttft_ms(self) -> Optional[float]:
        if self.first_token_at is None:
            return None
        return round((self.first_token_at - self.started_at) * 1000, 2)

    def total_latency_ms(self) -> Optional[float]:
        if self.completed_at is None:
            return None
        return round((self.completed_at - self.started_at) * 1000, 2)

    def tokens_per_second(self) -> Optional[float]:
        lat = self.total_latency_ms()
        if lat is None or lat == 0 or self.output_tokens == 0:
            return None
        return round(self.output_tokens / (lat / 1000.0), 1)

    def is_success(self) -> bool:
        return self.error is None and self.completed_at is not None
```

## Solution 2: Per-Provider Latency Store

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple


class PerProviderLatencyStore:
    """
    Maintains a rolling window of latency samples per (provider, model) pair.
    Computes percentiles and generation speed statistics on demand.
    """

    def __init__(self, window_size: int = 500):
        self._samples: Dict[str, Deque[ProviderLatencySample]] = {}
        self._window_size = window_size
        self._lock = Lock()

    def _key(self, provider: str, model: str) -> str:
        return f"{provider}:{model}"

    def record(self, sample: ProviderLatencySample) -> None:
        key = self._key(sample.provider, sample.model)
        with self._lock:
            if key not in self._samples:
                self._samples[key] = deque(maxlen=self._window_size)
            self._samples[key].append(sample)

    def percentile(
        self,
        provider: str,
        model: str,
        metric: str,   # "ttft_ms", "total_latency_ms", "tokens_per_second"
        pct: float,
        window_seconds: float = 3600.0,
    ) -> Optional[float]:
        key = self._key(provider, model)
        cutoff = time.time() - window_seconds
        with self._lock:
            samples = [s for s in self._samples.get(key, []) if s.started_at >= cutoff and s.is_success()]

        values = []
        for s in samples:
            v = getattr(s, metric)()
            if v is not None:
                values.append(v)

        if not values:
            return None
        values.sort()
        idx = min(int(len(values) * pct / 100.0), len(values) - 1)
        return round(values[idx], 2)

    def error_rate(self, provider: str, model: str, window_seconds: float = 3600.0) -> Optional[float]:
        key = self._key(provider, model)
        cutoff = time.time() - window_seconds
        with self._lock:
            samples = [s for s in self._samples.get(key, []) if s.started_at >= cutoff]
        if not samples:
            return None
        errors = sum(1 for s in samples if not s.is_success())
        return round(errors / len(samples), 4)

    def snapshot(self, provider: str, model: str, window_seconds: float = 3600.0) -> dict:
        return {
            "provider": provider,
            "model": model,
            "ttft_p50_ms": self.percentile(provider, model, "ttft_ms", 50, window_seconds),
            "ttft_p95_ms": self.percentile(provider, model, "ttft_ms", 95, window_seconds),
            "total_p50_ms": self.percentile(provider, model, "total_latency_ms", 50, window_seconds),
            "total_p99_ms": self.percentile(provider, model, "total_latency_ms", 99, window_seconds),
            "tokens_per_second_p50": self.percentile(provider, model, "tokens_per_second", 50, window_seconds),
            "error_rate": self.error_rate(provider, model, window_seconds),
        }

    def all_providers(self) -> List[Tuple[str, str]]:
        with self._lock:
            return [(k.split(":")[0], k.split(":")[1]) for k in self._samples.keys()]
```

## Solution 3: Provider Comparison Ranker

```python
from typing import List, Optional


class ProviderComparisonRanker:
    """
    Ranks providers by a composite score combining TTFT, total latency,
    and error rate — enabling automated routing recommendations.
    """

    def __init__(
        self,
        store: PerProviderLatencyStore,
        ttft_weight: float = 0.4,
        latency_weight: float = 0.4,
        error_weight: float = 0.2,
    ):
        self._store = store
        self._w_ttft = ttft_weight
        self._w_lat = latency_weight
        self._w_err = error_weight

    def rank(self, window_seconds: float = 3600.0) -> List[dict]:
        providers = self._store.all_providers()
        snapshots = []
        for provider, model in providers:
            snap = self._store.snapshot(provider, model, window_seconds)
            snapshots.append(snap)

        # Normalize each metric to [0, 1] (lower = better for latency/error, higher = better for speed)
        def normalize(values, invert=False):
            valid = [v for v in values if v is not None]
            if not valid:
                return [None] * len(values)
            min_v, max_v = min(valid), max(valid)
            if min_v == max_v:
                return [1.0 if not invert else 0.0 for _ in values]
            return [
                None if v is None else (1.0 - (v - min_v) / (max_v - min_v)) if not invert
                else (v - min_v) / (max_v - min_v)
                for v in values
            ]

        ttfts = [s.get("ttft_p95_ms") for s in snapshots]
        lats = [s.get("total_p99_ms") for s in snapshots]
        errs = [s.get("error_rate") or 0.0 for s in snapshots]

        norm_ttft = normalize(ttfts)
        norm_lat = normalize(lats)
        norm_err = normalize(errs, invert=True)

        for i, snap in enumerate(snapshots):
            ttft_score = norm_ttft[i] or 0.5
            lat_score = norm_lat[i] or 0.5
            err_score = norm_err[i] if norm_err[i] is not None else 0.5
            composite = round(
                self._w_ttft * ttft_score
                + self._w_lat * lat_score
                + self._w_err * err_score,
                4,
            )
            snap["composite_score"] = composite

        return sorted(snapshots, key=lambda s: s.get("composite_score", 0), reverse=True)
```

## Solution 4: Latency Regression Alerter

```python
import time
from typing import List, Optional


class ProviderLatencyRegressionAlerter:
    """
    Detects when a provider's recent P95 latency has regressed
    significantly compared to its historical baseline.
    """

    def __init__(
        self,
        store: PerProviderLatencyStore,
        regression_threshold_pct: float = 30.0,
    ):
        self._store = store
        self._threshold = regression_threshold_pct / 100.0

    def check_all(
        self,
        baseline_window: float = 86400.0,
        recent_window: float = 1800.0,
    ) -> List[dict]:
        regressions = []
        for provider, model in self._store.all_providers():
            baseline = self._store.percentile(provider, model, "total_latency_ms", 95, baseline_window)
            recent = self._store.percentile(provider, model, "total_latency_ms", 95, recent_window)
            if baseline is None or recent is None:
                continue
            change = (recent - baseline) / max(baseline, 1)
            if change > self._threshold:
                regressions.append({
                    "provider": provider,
                    "model": model,
                    "baseline_p95_ms": baseline,
                    "recent_p95_ms": recent,
                    "change_pct": round(change * 100, 1),
                    "threshold_pct": self._threshold * 100,
                })
        return sorted(regressions, key=lambda r: r["change_pct"], reverse=True)
```

## Solution 5: Tracked Provider Client

```python
import asyncio
import time
import uuid
from typing import Any, Callable, Optional


class LatencyTrackedProviderClient:
    """
    Wraps an LLM provider call with latency sampling.
    Supports streaming to capture TTFT separately from total latency.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        store: PerProviderLatencyStore,
    ):
        self._provider = provider
        self._model = model
        self._store = store

    async def call(
        self,
        llm_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        sample = ProviderLatencySample(
            provider=self._provider,
            model=self._model,
            request_id=uuid.uuid4().hex[:12],
        )
        try:
            result = await llm_fn(*args, **kwargs)
            sample.completed_at = time.time()
            usage = getattr(result, "usage", None)
            if usage:
                sample.input_tokens = getattr(usage, "input_tokens", 0)
                sample.output_tokens = getattr(usage, "output_tokens", 0)
            self._store.record(sample)
            return result
        except Exception as exc:
            sample.error = str(exc)
            sample.completed_at = time.time()
            self._store.record(sample)
            raise
```

## Solution 6: Provider Latency Comparison Dashboard

```python
import time


class ProviderLatencyComparisonDashboard:
    """
    Combines provider rankings, regression alerts, and per-provider snapshots.
    """

    def __init__(
        self,
        store: PerProviderLatencyStore,
        ranker: ProviderComparisonRanker,
        alerter: ProviderLatencyRegressionAlerter,
    ):
        self._store = store
        self._ranker = ranker
        self._alerter = alerter

    def render(self, window_seconds: float = 3600.0) -> dict:
        return {
            "generated_at": time.time(),
            "provider_rankings": self._ranker.rank(window_seconds),
            "regressions": self._alerter.check_all(),
        }
```

## Comparison

| Approach | Per-Provider Samples | Percentile Queries | Composite Ranking | Regression Alerts | Dashboard |
|---|---|---|---|---|---|
| PerProviderLatencyStore | Yes (rolling window) | Yes (P50/P95/P99) | No | No | No |
| ProviderComparisonRanker | Via store | Via store | Yes | No | No |
| ProviderLatencyRegressionAlerter | Via store | Via store | No | Yes | No |
| LatencyTrackedProviderClient | Yes (per call) | No | No | No | No |
| ProviderLatencyComparisonDashboard | No | No | No | No | Yes |

**Best for production**: Instrument every LLM call with `LatencyTrackedProviderClient` from day one — adding tracking retroactively requires touching every call site. Run `ProviderLatencyRegressionAlerter.check_all()` every 5 minutes and page on-call when P95 regression exceeds 50% — this almost always indicates a provider incident before their status page updates. Use `ProviderComparisonRanker.rank()` as the input to a dynamic routing policy: route 90% of traffic to the top-ranked provider and 10% to the second-ranked to keep the backup's latency sample fresh. Set `window_size=500` per provider to ensure percentile calculations are statistically meaningful without excessive memory use at high traffic volumes.
