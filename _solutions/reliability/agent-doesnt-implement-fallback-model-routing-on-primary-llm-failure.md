---
title: "Agent Doesn't Implement Fallback Model Routing on Primary LLM Failure"
description: "Agents that depend on a single LLM provider become completely unavailable when that provider experiences an outage, rate limit storm, or capacity event — even though alternative models could serve the request. Implement fallback model routing that detects primary model failures, routes requests to a configured sequence of alternative models, and restores primary routing automatically once the primary recovers."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-fallback-model-routing-on-primary-llm-failure
tags: [fallback-routing, multi-model, llm-failover, provider-redundancy, model-circuit-breaker, availability]
symptoms:
  - "Agent is completely unavailable whenever the primary LLM provider has an outage"
  - "Rate limit exhaustion on the primary provider blocks all requests with no fallback"
  - "No secondary model is configured — single-provider dependency with no redundancy"
  - "Provider capacity events during peak hours cause agent-wide degradation"
  - "Recovery from primary provider requires manual intervention to re-enable routing"
---

## Why This Happens

Most agent implementations hard-code a single model identifier and call the same provider endpoint for every request. When that endpoint is unavailable — due to provider outage, rate limit exhaustion, or model deprecation — the agent has no alternative path. Fallback routing treats model selection as a routing decision: a primary model is tried first, and if it fails (with a retryable error), the router advances to the next model in the fallback chain. Each model in the chain may be from a different provider, have different capabilities, or operate at a different cost point, giving the operator control over the trade-off between availability and cost during failover.

## Solution 1: Model Route

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ModelProvider(str, Enum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GOOGLE = "google"
    COHERE = "cohere"
    LOCAL = "local"


@dataclass
class ModelRoute:
    model_id: str
    provider: ModelProvider
    priority: int = 0                  # lower = higher priority
    max_tokens: Optional[int] = None
    supports_tools: bool = True
    supports_streaming: bool = True
    cost_per_1k_tokens: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    health_check_fn: Optional[Any] = None


@dataclass
class FallbackChain:
    name: str
    routes: List[ModelRoute]           # ordered by priority
    retry_primary_after_seconds: float = 300.0  # try primary again after this delay

    def primary(self) -> ModelRoute:
        return self.routes[0]

    def fallbacks(self) -> List[ModelRoute]:
        return self.routes[1:]
```

## Solution 2: Model Health Monitor

```python
import asyncio
import time
import threading
from typing import Dict, Optional


class ModelHealthMonitor:
    """
    Tracks the health of each model route based on recent call outcomes.
    Marks a model as unhealthy after consecutive failures and restores
    it after a cooldown period.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_seconds: float = 300.0,
    ):
        self._threshold = failure_threshold
        self._recovery = recovery_seconds
        self._failures: Dict[str, int] = {}
        self._unhealthy_since: Dict[str, float] = {}
        self._lock = threading.Lock()

    def record_success(self, model_id: str) -> None:
        with self._lock:
            self._failures[model_id] = 0
            self._unhealthy_since.pop(model_id, None)

    def record_failure(self, model_id: str) -> bool:
        """Returns True if model just became unhealthy."""
        with self._lock:
            self._failures[model_id] = self._failures.get(model_id, 0) + 1
            if self._failures[model_id] >= self._threshold:
                if model_id not in self._unhealthy_since:
                    self._unhealthy_since[model_id] = time.time()
                    return True
        return False

    def is_healthy(self, model_id: str) -> bool:
        with self._lock:
            since = self._unhealthy_since.get(model_id)
            if since is None:
                return True
            if time.time() - since >= self._recovery:
                # Recovered — reset and allow a probe
                self._unhealthy_since.pop(model_id, None)
                self._failures[model_id] = 0
                return True
            return False

    def all_health(self) -> Dict[str, bool]:
        return {
            model_id: self.is_healthy(model_id)
            for model_id in list(self._failures.keys())
        }
```

## Solution 3: Fallback Router

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class FallbackRouter:
    """
    Routes LLM calls through a fallback chain. Tries each model in order,
    skipping unhealthy models and advancing to the next on failure.
    Records outcomes back to the health monitor.
    """

    RETRYABLE_ERRORS = {
        "rate_limit", "overloaded", "timeout", "service_unavailable",
        "capacity", "503", "429", "502",
    }

    def __init__(
        self,
        chain: FallbackChain,
        monitor: ModelHealthMonitor,
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._chain = chain
        self._monitor = monitor
        self._audit = audit_fn or (lambda ev: None)
        self._primary_calls = 0
        self._fallback_calls = 0
        self._total_failures = 0

    def _is_retryable(self, exc: Exception) -> bool:
        msg = str(exc).lower()
        return any(code in msg for code in self.RETRYABLE_ERRORS)

    async def call(
        self,
        call_fn: Callable,          # async fn(model_id, **kwargs) -> Any
        **kwargs: Any,
    ) -> dict:
        healthy_routes = [
            r for r in self._chain.routes
            if self._monitor.is_healthy(r.model_id)
        ]

        if not healthy_routes:
            # All models unhealthy — try primary anyway as last resort
            healthy_routes = [self._chain.primary()]

        last_error = None
        for i, route in enumerate(healthy_routes):
            is_primary = route.model_id == self._chain.primary().model_id
            start = time.time()
            try:
                result = await call_fn(model_id=route.model_id, **kwargs)
                latency_ms = round((time.time() - start) * 1000, 2)
                self._monitor.record_success(route.model_id)
                if is_primary:
                    self._primary_calls += 1
                else:
                    self._fallback_calls += 1
                self._audit({
                    "event": "model_call_success",
                    "model_id": route.model_id,
                    "is_fallback": not is_primary,
                    "attempt": i + 1,
                    "latency_ms": latency_ms,
                    "timestamp": time.time(),
                })
                return {"result": result, "model_id": route.model_id, "is_fallback": not is_primary}
            except Exception as exc:
                latency_ms = round((time.time() - start) * 1000, 2)
                last_error = exc
                self._total_failures += 1
                became_unhealthy = self._monitor.record_failure(route.model_id)
                self._audit({
                    "event": "model_call_failed",
                    "model_id": route.model_id,
                    "error": str(exc),
                    "retryable": self._is_retryable(exc),
                    "became_unhealthy": became_unhealthy,
                    "latency_ms": latency_ms,
                    "timestamp": time.time(),
                })
                if not self._is_retryable(exc):
                    raise   # non-retryable errors propagate immediately

        raise FallbackExhaustedError(self._chain.name, last_error)

    def stats(self) -> dict:
        return {
            "primary_calls": self._primary_calls,
            "fallback_calls": self._fallback_calls,
            "fallback_rate": round(
                self._fallback_calls / max(self._primary_calls + self._fallback_calls, 1),
                4,
            ),
            "total_failures": self._total_failures,
        }


class FallbackExhaustedError(Exception):
    def __init__(self, chain_name: str, last_error: Optional[Exception]):
        super().__init__(
            f"all models in chain '{chain_name}' failed; last error: {last_error}"
        )
        self.chain_name = chain_name
        self.last_error = last_error
```

## Solution 4: Capability-Aware Route Selector

```python
from typing import List, Optional


class CapabilityAwareRouteSelector:
    """
    Filters the fallback chain to routes that support the required
    capabilities (tool use, streaming, minimum context length).
    """

    def __init__(self, chain: FallbackChain, monitor: ModelHealthMonitor):
        self._chain = chain
        self._monitor = monitor

    def select(
        self,
        requires_tools: bool = False,
        requires_streaming: bool = False,
    ) -> List[ModelRoute]:
        candidates = [
            r for r in self._chain.routes
            if self._monitor.is_healthy(r.model_id)
            and (not requires_tools or r.supports_tools)
            and (not requires_streaming or r.supports_streaming)
        ]
        return sorted(candidates, key=lambda r: r.priority)
```

## Solution 5: Primary Recovery Prober

```python
import asyncio
import time
from typing import Callable, Optional


class PrimaryRecoveryProber:
    """
    Periodically sends probe requests to the primary model when it
    is marked unhealthy. Restores primary routing on the first success.
    """

    def __init__(
        self,
        chain: FallbackChain,
        monitor: ModelHealthMonitor,
        probe_fn: Callable,
        probe_interval_seconds: float = 60.0,
    ):
        self._primary = chain.primary()
        self._monitor = monitor
        self._probe_fn = probe_fn
        self._interval = probe_interval_seconds
        self._running = False
        self._probes_sent = 0
        self._recoveries = 0

    async def run(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(self._interval)
            if self._monitor.is_healthy(self._primary.model_id):
                continue
            self._probes_sent += 1
            try:
                await self._probe_fn(model_id=self._primary.model_id)
                self._monitor.record_success(self._primary.model_id)
                self._recoveries += 1
            except Exception:
                pass   # still unhealthy

    def stop(self) -> None:
        self._running = False

    def stats(self) -> dict:
        return {
            "probes_sent": self._probes_sent,
            "recoveries_detected": self._recoveries,
        }
```

## Solution 6: Fallback Routing Dashboard

```python
import time


class FallbackRoutingDashboard:
    """
    Combines router statistics, model health, and prober status
    into a single operational view.
    """

    def __init__(
        self,
        router: FallbackRouter,
        monitor: ModelHealthMonitor,
        prober: PrimaryRecoveryProber,
        chain: FallbackChain,
    ):
        self._router = router
        self._monitor = monitor
        self._prober = prober
        self._chain = chain

    def render(self) -> dict:
        model_health = {
            r.model_id: {
                "provider": r.provider.value,
                "healthy": self._monitor.is_healthy(r.model_id),
                "is_primary": r.model_id == self._chain.primary().model_id,
            }
            for r in self._chain.routes
        }
        return {
            "generated_at": time.time(),
            "chain_name": self._chain.name,
            "router_stats": self._router.stats(),
            "model_health": model_health,
            "prober_stats": self._prober.stats(),
        }
```

## Comparison

| Approach | Health Tracking | Fallback Advancement | Capability Filtering | Auto-Recovery | Dashboard |
|---|---|---|---|---|---|
| ModelHealthMonitor | Yes (failure count) | No | No | Yes (TTL reset) | No |
| FallbackRouter | Via monitor | Yes (chain order) | No | Via monitor | No |
| CapabilityAwareRouteSelector | Via monitor | No | Yes | No | No |
| PrimaryRecoveryProber | No | No | No | Yes (active probe) | No |
| FallbackRoutingDashboard | No | No | No | No | Yes |

**Best for production**: Configure at least one fallback from a different provider than the primary — a same-provider fallback (e.g., GPT-4 falling back to GPT-3.5) does not protect against provider-wide outages. Set `retry_primary_after_seconds=300` and deploy `PrimaryRecoveryProber` to detect recovery within 1 minute of the primary coming back up. Alert when `fallback_rate` exceeds 5% over a 1-hour window — sustained fallback usage means the primary is degraded and the cost difference between models is accumulating silently.
