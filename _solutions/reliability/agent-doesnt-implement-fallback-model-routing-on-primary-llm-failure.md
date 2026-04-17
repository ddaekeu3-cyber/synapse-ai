---
title: "Agent Doesn't Implement Fallback Model Routing on Primary LLM Failure"
description: "Agents with a single LLM provider experience complete outages when that provider has an incident: a Claude API outage means zero agent responses until the incident resolves. Implement fallback model routing that detects primary LLM failure and transparently routes to a secondary provider, with quality-aware routing that returns to the primary once it recovers."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-fallback-model-routing-on-primary-llm-failure
tags: [fallback-routing, llm-failover, multi-provider, model-routing, outage-resilience, provider-redundancy]
symptoms:
  - "All agent requests fail when the primary LLM provider has an incident"
  - "No automatic recovery when the primary provider comes back online"
  - "Fallback is manual — engineers edit config and redeploy during incidents"
  - "No tracking of which provider is currently serving requests"
  - "Latency difference between primary and fallback is invisible in metrics"
---

## Why This Happens

Single-provider LLM integrations treat the model API as infinitely available. When the provider experiences an outage, rate limiting, or degraded latency, the agent has no recourse. Fallback routing requires at least two provider integrations and a health-tracking layer that promotes the fallback to primary when the primary is unhealthy. The challenge is that LLM providers have different APIs, token limits, and output characteristics — the routing layer must normalize these differences so the agent code does not change based on which provider is active.

## Solution 1: LLM Provider Interface

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ProviderStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass
class LLMRequest:
    messages: List[Dict[str, str]]
    max_tokens: int = 4096
    temperature: float = 0.0
    system: str = ""
    tools: List[dict] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    content: str
    provider_name: str
    model_id: str
    input_tokens: int
    output_tokens: int
    latency_ms: float
    finish_reason: str = "stop"
    tool_calls: List[dict] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)


class LLMProviderError(Exception):
    def __init__(self, provider: str, status_code: int, message: str):
        super().__init__(f"[{provider}] HTTP {status_code}: {message}")
        self.provider = provider
        self.status_code = status_code
        self.retryable = status_code in (429, 500, 502, 503, 529)
```

## Solution 2: Provider Health Tracker

```python
import time
from threading import Lock
from typing import Dict, List, Tuple


class ProviderHealthTracker:
    """
    Tracks success and failure rates per provider using a sliding window.
    Marks providers as degraded or unavailable based on error thresholds.
    """

    def __init__(
        self,
        window_seconds: int = 120,
        unavailable_threshold: float = 0.8,   # 80%+ errors = unavailable
        degraded_threshold: float = 0.3,      # 30%+ errors = degraded
        min_samples: int = 5,
    ):
        self._window = window_seconds
        self._unavailable = unavailable_threshold
        self._degraded = degraded_threshold
        self._min_samples = min_samples
        self._events: Dict[str, List[Tuple[float, bool]]] = {}
        self._lock = Lock()

    def record(self, provider_name: str, success: bool) -> None:
        with self._lock:
            if provider_name not in self._events:
                self._events[provider_name] = []
            self._events[provider_name].append((time.time(), success))

    def error_rate(self, provider_name: str) -> Tuple[float, int]:
        now = time.time()
        cutoff = now - self._window
        with self._lock:
            events = [(ts, ok) for ts, ok in self._events.get(provider_name, []) if ts >= cutoff]
        if len(events) < self._min_samples:
            return 0.0, len(events)
        errors = sum(1 for _, ok in events if not ok)
        return errors / len(events), len(events)

    def status(self, provider_name: str) -> ProviderStatus:
        rate, count = self.error_rate(provider_name)
        if count < self._min_samples:
            return ProviderStatus.HEALTHY
        if rate >= self._unavailable:
            return ProviderStatus.UNAVAILABLE
        if rate >= self._degraded:
            return ProviderStatus.DEGRADED
        return ProviderStatus.HEALTHY

    def all_statuses(self) -> Dict[str, ProviderStatus]:
        with self._lock:
            providers = list(self._events.keys())
        return {p: self.status(p) for p in providers}
```

## Solution 3: Fallback Model Router

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class FallbackModelRouter:
    """
    Routes LLM requests to providers in priority order.
    Skips unhealthy providers and records outcomes for health tracking.
    Falls back to the next available provider on failure.
    """

    def __init__(
        self,
        providers: List[Dict],     # [{"name": str, "call_fn": async callable, "priority": int}]
        health_tracker: ProviderHealthTracker,
        skip_degraded: bool = False,  # if True, skip degraded providers too
    ):
        self._providers = sorted(providers, key=lambda p: p["priority"])
        self._health = health_tracker
        self._skip_degraded = skip_degraded
        self._route_counts: Dict[str, int] = {}
        self._fallback_count = 0

    def _available_providers(self) -> list:
        result = []
        for provider in self._providers:
            status = self._health.status(provider["name"])
            if status == ProviderStatus.UNAVAILABLE:
                continue
            if self._skip_degraded and status == ProviderStatus.DEGRADED:
                continue
            result.append(provider)
        return result

    async def route(self, request: LLMRequest) -> LLMResponse:
        available = self._available_providers()
        if not available:
            available = self._providers   # last resort: try all

        last_error = None
        primary_name = self._providers[0]["name"] if self._providers else ""
        used_fallback = False

        for i, provider in enumerate(available):
            name = provider["name"]
            if i > 0:
                used_fallback = True
                self._fallback_count += 1

            start = time.time()
            try:
                response = await provider["call_fn"](request)
                self._health.record(name, success=True)
                self._route_counts[name] = self._route_counts.get(name, 0) + 1
                response.metadata["fallback_used"] = used_fallback
                return response
            except LLMProviderError as exc:
                latency_ms = (time.time() - start) * 1000
                self._health.record(name, success=False)
                last_error = exc
                if not exc.retryable:
                    raise
            except Exception as exc:
                self._health.record(name, success=False)
                last_error = exc

        raise last_error or RuntimeError("all LLM providers failed")

    def stats(self) -> dict:
        total = sum(self._route_counts.values())
        return {
            "route_counts": self._route_counts,
            "total_routed": total,
            "fallback_count": self._fallback_count,
            "fallback_rate": round(self._fallback_count / max(total, 1), 4),
            "provider_statuses": {
                p["name"]: self._health.status(p["name"]).value
                for p in self._providers
            },
        }
```

## Solution 4: Provider Recovery Monitor

```python
import asyncio
import time
from typing import Callable, List, Optional


class ProviderRecoveryMonitor:
    """
    Periodically probes unhealthy providers with lightweight health checks.
    When a provider recovers, emits a callback so the router can prefer it again.
    """

    def __init__(
        self,
        health_tracker: ProviderHealthTracker,
        probe_interval_seconds: float = 30.0,
        recovery_callback: Optional[Callable] = None,
    ):
        self._health = health_tracker
        self._interval = probe_interval_seconds
        self._recovery_callback = recovery_callback
        self._running = False
        self._probe_results: List[dict] = []

    async def probe_provider(self, provider_name: str, probe_fn: Callable) -> bool:
        try:
            await probe_fn()
            self._health.record(provider_name, success=True)
            return True
        except Exception:
            self._health.record(provider_name, success=False)
            return False

    async def run_loop(self, providers: List[dict]) -> None:
        self._running = True
        prev_statuses = {}

        while self._running:
            for provider in providers:
                name = provider["name"]
                prev = prev_statuses.get(name, ProviderStatus.HEALTHY)
                current = self._health.status(name)

                if current == ProviderStatus.UNAVAILABLE and provider.get("probe_fn"):
                    recovered = await self.probe_provider(name, provider["probe_fn"])
                    if recovered and prev == ProviderStatus.UNAVAILABLE:
                        if self._recovery_callback:
                            await self._recovery_callback(name)

                prev_statuses[name] = current

            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False
```

## Solution 5: Response Normalizer

```python
from typing import Any, Dict


class LLMResponseNormalizer:
    """
    Normalizes responses from different LLM providers into a consistent
    LLMResponse format so upstream code is provider-agnostic.
    """

    @staticmethod
    def from_anthropic(raw: dict, provider_name: str, latency_ms: float) -> LLMResponse:
        content_blocks = raw.get("content", [])
        text = " ".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
        tool_calls = [b for b in content_blocks if b.get("type") == "tool_use"]
        usage = raw.get("usage", {})
        return LLMResponse(
            content=text,
            provider_name=provider_name,
            model_id=raw.get("model", ""),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            latency_ms=latency_ms,
            finish_reason=raw.get("stop_reason", "stop"),
            tool_calls=tool_calls,
        )

    @staticmethod
    def from_openai(raw: dict, provider_name: str, latency_ms: float) -> LLMResponse:
        choice = raw.get("choices", [{}])[0]
        message = choice.get("message", {})
        usage = raw.get("usage", {})
        tool_calls = message.get("tool_calls", [])
        return LLMResponse(
            content=message.get("content", "") or "",
            provider_name=provider_name,
            model_id=raw.get("model", ""),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
            finish_reason=choice.get("finish_reason", "stop"),
            tool_calls=tool_calls,
        )
```

## Solution 6: Fallback Routing Dashboard

```python
import time


class FallbackRoutingDashboard:
    """
    Combines router stats, provider health, and recovery monitor state
    into a single operational view.
    """

    def __init__(
        self,
        router: FallbackModelRouter,
        health_tracker: ProviderHealthTracker,
    ):
        self._router = router
        self._health = health_tracker

    def render(self) -> dict:
        stats = self._router.stats()
        all_statuses = self._health.all_statuses()
        degraded = [n for n, s in all_statuses.items() if s == ProviderStatus.DEGRADED]
        unavailable = [n for n, s in all_statuses.items() if s == ProviderStatus.UNAVAILABLE]

        return {
            "generated_at": time.time(),
            "router_stats": stats,
            "provider_health": {k: v.value for k, v in all_statuses.items()},
            "degraded_providers": degraded,
            "unavailable_providers": unavailable,
            "alert": len(unavailable) > 0 or stats["fallback_rate"] > 0.1,
        }
```

## Comparison

| Approach | Priority Routing | Health Tracking | Fallback | Recovery Detection | Response Normalization |
|---|---|---|---|---|---|
| ProviderHealthTracker | No | Yes (sliding window) | No | No | No |
| FallbackModelRouter | Yes | Via tracker | Yes | No | No |
| ProviderRecoveryMonitor | No | Via tracker | No | Yes (probe) | No |
| LLMResponseNormalizer | No | No | No | No | Yes (Anthropic+OpenAI) |
| FallbackRoutingDashboard | No | No | No | No | Yes (aggregated) |

**Best for production**: Keep at least two providers with different infrastructure dependencies — using Claude as primary and GPT-4 as fallback (or vice versa) ensures that an AWS outage affecting Anthropic does not simultaneously affect OpenAI. Set `unavailable_threshold=0.8` with `min_samples=5` so the router does not prematurely failover on a single transient error. Run `ProviderRecoveryMonitor` with a lightweight health check (e.g., a minimal single-token completion) every 30 seconds so recovery is detected within a minute of the provider coming back. Alert when `fallback_rate > 0.05` — sustained fallback usage means the primary is degraded and incident response should begin.
