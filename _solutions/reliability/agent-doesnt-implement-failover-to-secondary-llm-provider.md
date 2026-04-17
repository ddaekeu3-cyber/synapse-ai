---
title: "Agent Doesn't Implement Failover to Secondary LLM Provider"
description: "Agents that depend on a single LLM provider experience full outages when that provider has an incident — a situation that occurs multiple times per year for every major provider. Implement automatic failover to a secondary LLM provider with model capability matching, response format normalization, and health-based routing that switches back to primary when it recovers."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-failover-to-secondary-llm-provider
tags: [llm-failover, provider-redundancy, multi-provider, health-based-routing, outage-resilience, provider-switching]
symptoms:
  - "Full agent outage when primary LLM provider has an incident"
  - "No automatic recovery when provider returns to health — manual restart required"
  - "Secondary provider never tested in production — failover path untested"
  - "Different providers return different response formats — no normalization layer"
  - "No circuit breaker on the provider level — failed calls pile up during outages"
---

## Why This Happens

LLM provider SLAs are typically 99.9% (8.7 hours downtime per year) and actual availability is often lower during major incidents. Agents that treat the LLM call as a simple HTTP request with retries will exhaust their retry budget during an outage and return errors to users. Provider failover requires a routing layer that tracks per-provider health, switches to a configured secondary when health degrades, normalizes response formats across providers, and probes for primary recovery so routing reverts automatically when the incident resolves.

## Solution 1: Provider Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ProviderStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class LLMProviderConfig:
    provider_id: str
    display_name: str
    base_url: str
    model_id: str               # model to use with this provider
    api_key_env_var: str        # env var name for the API key
    priority: int = 0           # lower = preferred; 0 = primary
    max_tokens: int = 4096
    supports_system_prompt: bool = True
    supports_streaming: bool = True
    response_format: str = "openai"   # "openai" | "anthropic" | "custom"
    extra_headers: Dict[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0
```

## Solution 2: Provider Health Tracker

```python
import time
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Deque, Optional, Tuple


@dataclass
class ProviderHealthSnapshot:
    provider_id: str
    status: ProviderStatus
    error_rate: float
    p95_latency_ms: float
    consecutive_failures: int
    last_success_at: Optional[float]
    last_failure_at: Optional[float]


class ProviderHealthTracker:
    """
    Tracks per-provider call outcomes and computes health status.
    Transitions to UNHEALTHY after consecutive failures and back to
    HEALTHY after successful probe responses.
    """

    def __init__(
        self,
        provider_id: str,
        window_seconds: float = 120.0,
        failure_threshold: int = 5,
        degraded_error_rate: float = 0.20,
    ):
        self._id = provider_id
        self._window = window_seconds
        self._failure_threshold = failure_threshold
        self._degraded_rate = degraded_error_rate
        self._outcomes: Deque[Tuple[float, bool, float]] = deque()
        # (ts, success, latency_ms)
        self._consecutive_failures = 0
        self._last_success: Optional[float] = None
        self._last_failure: Optional[float] = None
        self._lock = Lock()

    def record(self, success: bool, latency_ms: float) -> None:
        now = time.time()
        with self._lock:
            self._outcomes.append((now, success, latency_ms))
            cutoff = now - self._window
            while self._outcomes and self._outcomes[0][0] < cutoff:
                self._outcomes.popleft()
            if success:
                self._consecutive_failures = 0
                self._last_success = now
            else:
                self._consecutive_failures += 1
                self._last_failure = now

    def snapshot(self) -> ProviderHealthSnapshot:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            recent = [(ts, ok, lat) for ts, ok, lat in self._outcomes if ts >= cutoff]
            consec = self._consecutive_failures
            last_ok = self._last_success
            last_fail = self._last_failure

        if not recent:
            return ProviderHealthSnapshot(
                provider_id=self._id,
                status=ProviderStatus.HEALTHY,
                error_rate=0.0,
                p95_latency_ms=0.0,
                consecutive_failures=consec,
                last_success_at=last_ok,
                last_failure_at=last_fail,
            )

        errors = sum(1 for _, ok, _ in recent if not ok)
        rate = errors / len(recent)
        latencies = sorted(lat for _, _, lat in recent)
        p95_idx = min(int(len(latencies) * 0.95), len(latencies) - 1)
        p95 = latencies[p95_idx]

        if consec >= self._failure_threshold:
            status = ProviderStatus.UNHEALTHY
        elif rate >= self._degraded_rate:
            status = ProviderStatus.DEGRADED
        else:
            status = ProviderStatus.HEALTHY

        return ProviderHealthSnapshot(
            provider_id=self._id,
            status=status,
            error_rate=round(rate, 4),
            p95_latency_ms=round(p95, 2),
            consecutive_failures=consec,
            last_success_at=last_ok,
            last_failure_at=last_fail,
        )
```

## Solution 3: Normalized LLM Response

```python
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class NormalizedLLMResponse:
    content: str
    provider_id: str
    model_id: str
    input_tokens: int
    output_tokens: int
    finish_reason: str    # "stop" | "max_tokens" | "error"
    raw_response: Any = None
    latency_ms: float = 0.0

    @classmethod
    def from_openai(cls, raw: Any, provider_id: str, latency_ms: float) -> "NormalizedLLMResponse":
        choice = raw.choices[0]
        usage = raw.usage
        return cls(
            content=choice.message.content or "",
            provider_id=provider_id,
            model_id=raw.model,
            input_tokens=usage.prompt_tokens,
            output_tokens=usage.completion_tokens,
            finish_reason=choice.finish_reason or "stop",
            raw_response=raw,
            latency_ms=latency_ms,
        )

    @classmethod
    def from_anthropic(cls, raw: Any, provider_id: str, latency_ms: float) -> "NormalizedLLMResponse":
        content = "".join(b.text for b in raw.content if hasattr(b, "text"))
        usage = raw.usage
        return cls(
            content=content,
            provider_id=provider_id,
            model_id=raw.model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            finish_reason=raw.stop_reason or "stop",
            raw_response=raw,
            latency_ms=latency_ms,
        )
```

## Solution 4: Multi-Provider Router

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class MultiProviderRouter:
    """
    Routes LLM calls to the healthiest available provider.
    Falls back to secondary providers in priority order.
    Probes unhealthy providers periodically to detect recovery.
    """

    def __init__(
        self,
        configs: List[LLMProviderConfig],
        trackers: Dict[str, ProviderHealthTracker],
        call_fns: Dict[str, Callable],   # provider_id -> async call fn
        probe_interval_s: float = 30.0,
    ):
        self._configs = sorted(configs, key=lambda c: c.priority)
        self._trackers = trackers
        self._call_fns = call_fns
        self._probe_interval = probe_interval_s
        self._last_probe: Dict[str, float] = {}
        self._failover_count = 0

    def _select_provider(self) -> Optional[LLMProviderConfig]:
        for config in self._configs:
            snap = self._trackers[config.provider_id].snapshot()
            if snap.status != ProviderStatus.UNHEALTHY:
                return config
        return self._configs[0] if self._configs else None  # last resort

    async def call(
        self,
        messages: List[dict],
        **kwargs,
    ) -> NormalizedLLMResponse:
        providers_tried = []
        for config in self._configs:
            snap = self._trackers[config.provider_id].snapshot()
            if snap.status == ProviderStatus.UNHEALTHY:
                # Check if it is time to probe
                last = self._last_probe.get(config.provider_id, 0.0)
                if time.time() - last < self._probe_interval:
                    continue  # still in cooldown — skip
                self._last_probe[config.provider_id] = time.time()

            call_fn = self._call_fns.get(config.provider_id)
            if not call_fn:
                continue

            start = time.time()
            try:
                raw = await call_fn(messages, config, **kwargs)
                latency_ms = round((time.time() - start) * 1000, 2)
                self._trackers[config.provider_id].record(True, latency_ms)
                if providers_tried:
                    self._failover_count += 1
                return raw if isinstance(raw, NormalizedLLMResponse) else NormalizedLLMResponse(
                    content=str(raw), provider_id=config.provider_id,
                    model_id=config.model_id, input_tokens=0, output_tokens=0,
                    finish_reason="stop", latency_ms=latency_ms,
                )
            except Exception as exc:
                latency_ms = round((time.time() - start) * 1000, 2)
                self._trackers[config.provider_id].record(False, latency_ms)
                providers_tried.append(config.provider_id)

        raise RuntimeError(
            f"All LLM providers failed. Tried: {providers_tried}"
        )

    def stats(self) -> dict:
        return {
            "failover_count": self._failover_count,
            "provider_health": {
                config.provider_id: self._trackers[config.provider_id].snapshot().status.value
                for config in self._configs
            },
        }
```

## Solution 5: Provider Failover Event Logger

```python
import time
from typing import List


class ProviderFailoverEventLogger:
    """
    Records failover events for postmortem analysis and SLO reporting.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[dict] = []
        self._max = max_records

    def record_failover(
        self,
        from_provider: str,
        to_provider: str,
        reason: str,
        session_id: str = "",
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "event": "failover",
            "from": from_provider,
            "to": to_provider,
            "reason": reason,
            "session_id": session_id,
        })

    def record_recovery(self, provider_id: str) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "event": "recovery",
            "provider_id": provider_id,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        return {
            "window_seconds": window_seconds,
            "failover_events": sum(1 for r in recent if r["event"] == "failover"),
            "recovery_events": sum(1 for r in recent if r["event"] == "recovery"),
            "records": recent[-20:],
        }
```

## Solution 6: Multi-Provider Dashboard

```python
import time


class MultiProviderDashboard:
    """
    Combines provider health snapshots, router stats, and failover
    event history into a unified operational view.
    """

    def __init__(
        self,
        configs: List[LLMProviderConfig],
        trackers: Dict[str, ProviderHealthTracker],
        router: MultiProviderRouter,
        event_logger: ProviderFailoverEventLogger,
    ):
        self._configs = configs
        self._trackers = trackers
        self._router = router
        self._logger = event_logger

    def render(self) -> dict:
        health = {}
        for config in self._configs:
            snap = self._trackers[config.provider_id].snapshot()
            health[config.provider_id] = {
                "status": snap.status.value,
                "error_rate": snap.error_rate,
                "p95_latency_ms": snap.p95_latency_ms,
                "consecutive_failures": snap.consecutive_failures,
                "priority": config.priority,
            }
        return {
            "generated_at": time.time(),
            "provider_health": health,
            "router_stats": self._router.stats(),
            "failover_events_last_hour": self._logger.summary(3600.0),
        }
```

## Comparison

| Approach | Health Tracking | Priority Routing | Response Normalization | Probe Recovery | Failover Log |
|---|---|---|---|---|---|
| ProviderHealthTracker | Yes (sliding window) | No | No | No | No |
| MultiProviderRouter | Via trackers | Yes (priority order) | Via NormalizedLLMResponse | Yes (probe interval) | No |
| NormalizedLLMResponse | No | No | Yes (OpenAI + Anthropic) | No | No |
| ProviderFailoverEventLogger | No | No | No | No | Yes |
| MultiProviderDashboard | Via trackers | No | No | No | Via logger |

**Best for production**: Test the failover path monthly in production by deliberately marking the primary as unhealthy and verifying traffic routes to secondary — an untested failover path is not a failover path. Set `probe_interval_s=30` so recovery is detected within 30 seconds of primary restoration. Use `NormalizedLLMResponse` as the only return type from all provider call functions — never let provider-specific response objects leak past the router boundary. Monitor `failover_count` in `MultiProviderDashboard` — more than 3 failovers per day indicates primary reliability is below acceptable levels and a SLO conversation with the provider is warranted.
