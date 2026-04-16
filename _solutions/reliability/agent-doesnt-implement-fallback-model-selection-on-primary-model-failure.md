---
title: "Agent Doesn't Implement Fallback Model Selection on Primary Model Failure"
description: "Agents that route all LLM calls to a single model provider fail completely when that provider is unavailable, rate-limited, or returns degraded outputs. Implement fallback model selection that automatically tries alternative models in priority order, tracks per-model health, and routes traffic back to the primary once it recovers."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-fallback-model-selection-on-primary-model-failure
tags: [fallback-model, model-routing, provider-failover, llm-reliability, multi-model, circuit-breaker]
symptoms:
  - "All LLM calls fail when the primary provider has an outage"
  - "Rate limit errors from one provider cause complete agent unavailability"
  - "No automatic recovery when a degraded provider starts responding again"
  - "Agent has no visibility into which model is actually serving requests"
  - "Fallback is manual — operators must change config during incidents"
---

## Why This Happens

Most agents are built with a single model client instantiated at startup. When the provider's API returns 5xx errors or rate limit responses, the agent has no mechanism to switch to an alternative. Implementing fallback model selection requires a priority-ordered list of model configurations, a health tracker that records recent failure rates per model, a routing policy that skips unhealthy models, and a recovery probe that reinstates models once they pass a health check. The routing layer must be transparent — callers use the same interface regardless of which underlying model is serving the request.

## Solution 1: Model Candidate

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class ModelHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


@dataclass
class ModelCandidate:
    model_id: str
    provider: str
    priority: int              # lower = higher priority; 0 is primary
    client: Any                # the actual SDK client
    max_tokens: int = 4096
    extra_params: Dict[str, Any] = field(default_factory=dict)
    health: ModelHealth = ModelHealth.HEALTHY
    consecutive_failures: int = 0
    total_calls: int = 0
    total_failures: int = 0

    def failure_rate(self) -> float:
        if self.total_calls == 0:
            return 0.0
        return self.total_failures / self.total_calls
```

## Solution 2: Model Health Tracker

```python
import time
from collections import deque
from threading import Lock
from typing import Deque, Tuple


class ModelHealthTracker:
    """
    Tracks recent call outcomes for a single model candidate.
    Marks the model as degraded or unavailable based on failure rate
    within a sliding window.
    """

    def __init__(
        self,
        window_seconds: float = 120.0,
        degraded_threshold: float = 0.25,
        unavailable_threshold: float = 0.60,
        min_calls_for_threshold: int = 5,
        consecutive_unavailable: int = 3,
    ):
        self._window = window_seconds
        self._degraded_th = degraded_threshold
        self._unavailable_th = unavailable_threshold
        self._min_calls = min_calls_for_threshold
        self._consec_unavail = consecutive_unavailable
        self._events: Deque[Tuple[float, bool]] = deque()  # (ts, success)
        self._lock = Lock()

    def record(self, success: bool) -> None:
        with self._lock:
            now = time.time()
            self._events.append((now, success))
            cutoff = now - self._window
            while self._events and self._events[0][0] < cutoff:
                self._events.popleft()

    def current_health(self, candidate: "ModelCandidate") -> ModelHealth:
        with self._lock:
            if candidate.consecutive_failures >= self._consec_unavail:
                return ModelHealth.UNAVAILABLE
            if len(self._events) < self._min_calls:
                return ModelHealth.HEALTHY
            failures = sum(1 for _, ok in self._events if not ok)
            rate = failures / len(self._events)
            if rate >= self._unavailable_th:
                return ModelHealth.UNAVAILABLE
            if rate >= self._degraded_th:
                return ModelHealth.DEGRADED
            return ModelHealth.HEALTHY
```

## Solution 3: Fallback Model Router

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class FallbackModelRouter:
    """
    Maintains a priority-ordered list of model candidates and routes
    each LLM call to the highest-priority healthy model. Automatically
    tries the next candidate if the current one fails.
    """

    def __init__(
        self,
        candidates: List[ModelCandidate],
        health_trackers: Optional[Dict[str, ModelHealthTracker]] = None,
        recovery_probe_interval_seconds: float = 60.0,
    ):
        self._candidates = sorted(candidates, key=lambda c: c.priority)
        self._trackers: Dict[str, ModelHealthTracker] = health_trackers or {
            c.model_id: ModelHealthTracker() for c in candidates
        }
        self._probe_interval = recovery_probe_interval_seconds
        self._last_probe: Dict[str, float] = {}

    def _healthy_candidates(self) -> List[ModelCandidate]:
        result = []
        for c in self._candidates:
            tracker = self._trackers.get(c.model_id)
            if tracker:
                c.health = tracker.current_health(c)
            if c.health != ModelHealth.UNAVAILABLE:
                result.append(c)
        # always include at least one candidate to attempt recovery
        if not result:
            result = [self._candidates[0]]
        return result

    async def call(
        self,
        invoke_fn: Callable[[ModelCandidate], Any],
        *,
        context: str = "",
    ) -> dict:
        candidates = self._healthy_candidates()
        last_error: Optional[Exception] = None

        for candidate in candidates:
            candidate.total_calls += 1
            try:
                result = await invoke_fn(candidate)
                candidate.consecutive_failures = 0
                tracker = self._trackers.get(candidate.model_id)
                if tracker:
                    tracker.record(True)
                return {
                    "result": result,
                    "model_id": candidate.model_id,
                    "provider": candidate.provider,
                    "was_fallback": candidate.priority > 0,
                }
            except Exception as exc:
                candidate.consecutive_failures += 1
                candidate.total_failures += 1
                tracker = self._trackers.get(candidate.model_id)
                if tracker:
                    tracker.record(False)
                last_error = exc

        raise RuntimeError(
            f"All {len(candidates)} model candidate(s) failed. Last error: {last_error}"
        )

    def active_model(self) -> Optional[ModelCandidate]:
        healthy = self._healthy_candidates()
        return healthy[0] if healthy else None
```

## Solution 4: Recovery Probe Scheduler

```python
import asyncio
import time
from typing import Callable, Dict


class ModelRecoveryProbeScheduler:
    """
    Periodically probes unavailable models with a lightweight call
    to check if they have recovered. Resets consecutive_failures on
    a successful probe so the router can route traffic back to them.
    """

    def __init__(
        self,
        router: FallbackModelRouter,
        probe_fn: Callable[[ModelCandidate], Any],
        probe_interval_seconds: float = 60.0,
    ):
        self._router = router
        self._probe_fn = probe_fn
        self._interval = probe_interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while self._running:
            await asyncio.sleep(self._interval)
            for candidate in self._router._candidates:
                if candidate.health == ModelHealth.UNAVAILABLE:
                    try:
                        await self._probe_fn(candidate)
                        candidate.consecutive_failures = 0
                        tracker = self._router._trackers.get(candidate.model_id)
                        if tracker:
                            tracker.record(True)
                    except Exception:
                        pass  # still unavailable
```

## Solution 5: Fallback-Aware LLM Client

```python
from typing import Any, Dict, List, Optional


class FallbackAwareLLMClient:
    """
    Drop-in LLM client that wraps FallbackModelRouter.
    Callers use a single .complete() method and receive
    model routing metadata in the response envelope.
    """

    def __init__(
        self,
        router: FallbackModelRouter,
        default_system_prompt: str = "",
    ):
        self._router = router
        self._default_system = default_system_prompt
        self._call_log: List[dict] = []

    async def complete(
        self,
        messages: List[Dict[str, str]],
        *,
        max_tokens: Optional[int] = None,
        system: Optional[str] = None,
    ) -> dict:
        sys_prompt = system or self._default_system

        async def invoke(candidate: ModelCandidate) -> Any:
            params = {
                "model": candidate.model_id,
                "messages": messages,
                "max_tokens": max_tokens or candidate.max_tokens,
                **candidate.extra_params,
            }
            if sys_prompt:
                params["system"] = sys_prompt
            return await candidate.client.messages.create(**params)

        response = await self._router.call(invoke)
        self._call_log.append({
            "model_id": response["model_id"],
            "was_fallback": response["was_fallback"],
        })
        return response

    def routing_summary(self) -> dict:
        total = len(self._call_log)
        fallbacks = sum(1 for r in self._call_log if r["was_fallback"])
        by_model: Dict[str, int] = {}
        for r in self._call_log:
            by_model[r["model_id"]] = by_model.get(r["model_id"], 0) + 1
        return {
            "total_calls": total,
            "fallback_calls": fallbacks,
            "fallback_rate": round(fallbacks / max(total, 1), 4),
            "by_model": by_model,
        }
```

## Solution 6: Model Fallback Dashboard

```python
import time
from typing import List


class ModelFallbackDashboard:
    """
    Renders a snapshot of all model candidate health states,
    recent fallback rates, and active routing decisions.
    """

    def __init__(
        self,
        router: FallbackModelRouter,
        client: FallbackAwareLLMClient,
    ):
        self._router = router
        self._client = client

    def render(self) -> dict:
        candidates_status = []
        for c in self._router._candidates:
            tracker = self._router._trackers.get(c.model_id)
            health = tracker.current_health(c) if tracker else c.health
            candidates_status.append({
                "model_id": c.model_id,
                "provider": c.provider,
                "priority": c.priority,
                "health": health.value,
                "consecutive_failures": c.consecutive_failures,
                "failure_rate": round(c.failure_rate(), 4),
                "total_calls": c.total_calls,
            })

        active = self._router.active_model()
        return {
            "generated_at": time.time(),
            "active_model": active.model_id if active else None,
            "candidates": candidates_status,
            "routing_summary": self._client.routing_summary(),
        }
```

## Comparison

| Approach | Priority Routing | Health Tracking | Fallback Attempt | Recovery Probe | Routing Metrics |
|---|---|---|---|---|---|
| ModelHealthTracker | No | Yes (sliding window) | No | No | No |
| FallbackModelRouter | Yes | Via trackers | Yes (ordered) | No | No |
| ModelRecoveryProbeScheduler | No | Via router | No | Yes (async) | No |
| FallbackAwareLLMClient | Via router | Via router | Via router | No | Yes |
| ModelFallbackDashboard | No | No | No | No | Yes |

**Best for production**: Configure at least two candidates — a primary (priority=0) and a secondary from a different provider (priority=1). Set `consecutive_unavailable=3` so a single transient error does not trigger failover, but three consecutive failures do. Run `ModelRecoveryProbeScheduler` with `probe_interval_seconds=60` so the primary is reinstated within a minute of recovery rather than remaining on fallback indefinitely. Emit `was_fallback=true` as a structured log field on every call so dashboards can alert when fallback rate exceeds 5% — that threshold indicates the primary is degraded even if not fully unavailable.
