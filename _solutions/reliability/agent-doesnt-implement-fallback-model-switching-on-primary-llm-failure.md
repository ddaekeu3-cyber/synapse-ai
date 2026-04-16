---
title: "Agent Doesn't Implement Fallback Model Switching on Primary LLM Failure"
description: "Agents that depend on a single LLM provider become fully unavailable during provider outages, even when a comparable alternative model is accessible. Implement fallback model switching that detects primary model failures, transparently routes to a configured secondary model, and restores primary routing once the provider recovers — with capability-aware routing that only falls back to models supporting the required features."
date: 2026-04-16
difficulty: intermediate
category: reliability
slug: agent-doesnt-implement-fallback-model-switching-on-primary-llm-failure
tags: [fallback-model, llm-failover, provider-failover, model-routing, multi-provider, resilience]
symptoms:
  - "All agent sessions fail during an OpenAI outage even though Anthropic is available"
  - "No secondary model configured — single provider failure means total agent downtime"
  - "Fallback model used for tool-calling but primary model had tool-call support; fallback does not"
  - "Primary model recovers but agent continues using the slower, more expensive fallback"
  - "No visibility into which model is currently serving requests or why a switch occurred"
---

## Why This Happens

Agents are typically coded against a single model name. When the provider's API returns a 503, the call fails and the session errors out. Fallback requires a model routing layer that intercepts failures, selects an alternative from a priority-ordered list, and retries on the new model. Capability matching is essential: if the primary model supports tool calling and the fallback does not, routing to the fallback for a tool-calling request produces a worse failure than the original error.

## Solution 1: Model Capability Profile

```python
from dataclasses import dataclass, field
from typing import FrozenSet


@dataclass(frozen=True)
class ModelCapabilityProfile:
    model_id: str
    provider: str
    supports_tool_calling: bool = False
    supports_vision: bool = False
    supports_json_mode: bool = False
    max_context_tokens: int = 4096
    cost_per_1k_tokens: float = 0.0
    avg_latency_ms: float = 1000.0
    tier: str = "primary"   # "primary" | "secondary" | "emergency"

    def satisfies(self, required_capabilities: FrozenSet[str]) -> bool:
        cap_map = {
            "tool_calling": self.supports_tool_calling,
            "vision": self.supports_vision,
            "json_mode": self.supports_json_mode,
        }
        return all(cap_map.get(cap, False) for cap in required_capabilities)
```

## Solution 2: Model Route Table

```python
from typing import Dict, FrozenSet, List, Optional


class ModelRouteTable:
    """
    Ordered list of model profiles for fallback selection.
    Selects the highest-priority model that satisfies required capabilities
    and is not currently marked as failed.
    """

    def __init__(self, profiles: List[ModelCapabilityProfile]):
        self._profiles = profiles
        self._failed: Dict[str, float] = {}   # model_id -> failed_at timestamp

    def mark_failed(self, model_id: str) -> None:
        import time
        self._failed[model_id] = time.time()

    def mark_recovered(self, model_id: str) -> None:
        self._failed.pop(model_id, None)

    def is_failed(self, model_id: str, failure_window_seconds: float = 60.0) -> bool:
        import time
        failed_at = self._failed.get(model_id)
        if failed_at is None:
            return False
        return (time.time() - failed_at) < failure_window_seconds

    def select(
        self,
        required_capabilities: FrozenSet[str] = frozenset(),
        exclude_models: FrozenSet[str] = frozenset(),
        failure_window_seconds: float = 60.0,
    ) -> Optional[ModelCapabilityProfile]:
        for profile in self._profiles:
            if profile.model_id in exclude_models:
                continue
            if self.is_failed(profile.model_id, failure_window_seconds):
                continue
            if profile.satisfies(required_capabilities):
                return profile
        return None

    def all_profiles(self) -> List[ModelCapabilityProfile]:
        return list(self._profiles)
```

## Solution 3: Fallback Model Router

```python
import asyncio
import time
from typing import Any, Callable, Dict, FrozenSet, Optional


class FallbackModelRouter:
    """
    Routes LLM calls through a priority-ordered model list.
    On failure, marks the model as degraded and retries with the next candidate.
    Restores primary routing when the primary recovers (probed periodically).
    """

    def __init__(
        self,
        route_table: ModelRouteTable,
        failure_window_seconds: float = 60.0,
        recovery_probe_interval: float = 120.0,
    ):
        self._table = route_table
        self._failure_window = failure_window_seconds
        self._recovery_probe_interval = recovery_probe_interval
        self._active_model: Optional[str] = None
        self._switches: int = 0
        self._last_probe: Dict[str, float] = {}

    async def call(
        self,
        llm_factory: Callable[[str], Callable],   # model_id -> async call fn
        required_capabilities: FrozenSet[str] = frozenset(),
        **kwargs: Any,
    ) -> tuple:
        """Returns (result, model_id_used)."""
        tried: set = set()

        while True:
            profile = self._table.select(
                required_capabilities=required_capabilities,
                exclude_models=frozenset(tried),
                failure_window_seconds=self._failure_window,
            )
            if profile is None:
                raise RuntimeError(
                    f"No available model with capabilities {required_capabilities}. "
                    f"Tried: {tried}"
                )

            model_id = profile.model_id
            call_fn = llm_factory(model_id)

            try:
                result = await call_fn(**kwargs)
                if self._active_model != model_id:
                    self._active_model = model_id
                return result, model_id
            except Exception as exc:
                retryable = self._is_provider_error(exc)
                if not retryable:
                    raise
                self._table.mark_failed(model_id)
                self._switches += 1
                tried.add(model_id)

    def _is_provider_error(self, exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        if status:
            return int(status) in {429, 500, 502, 503, 504}
        msg = str(exc).lower()
        return any(kw in msg for kw in ["timeout", "connection", "unavailable", "overloaded"])

    async def probe_primary_recovery(
        self,
        llm_factory: Callable[[str], Callable],
        probe_fn: Callable,
    ) -> Optional[str]:
        """
        Probes failed models to detect recovery.
        Returns the model_id of the first recovered model, or None.
        """
        now = time.time()
        for profile in self._table.all_profiles():
            if not self._table.is_failed(profile.model_id):
                continue
            last_probe = self._last_probe.get(profile.model_id, 0)
            if now - last_probe < self._recovery_probe_interval:
                continue
            self._last_probe[profile.model_id] = now
            try:
                await probe_fn(llm_factory(profile.model_id))
                self._table.mark_recovered(profile.model_id)
                return profile.model_id
            except Exception:
                pass
        return None

    def stats(self) -> dict:
        return {
            "active_model": self._active_model,
            "model_switches": self._switches,
            "failed_models": [
                mid for mid in [p.model_id for p in self._table.all_profiles()]
                if self._table.is_failed(mid)
            ],
        }
```

## Solution 4: Capability-Aware Request Builder

```python
from typing import Any, Dict, FrozenSet, List


class CapabilityAwareRequestBuilder:
    """
    Adapts a generic LLM request to the capabilities of the target model.
    Strips tool definitions if the target model doesn't support tool calling.
    Converts tool calls to text instructions for models without tool support.
    """

    def adapt(
        self,
        request: Dict[str, Any],
        profile: ModelCapabilityProfile,
    ) -> Dict[str, Any]:
        adapted = dict(request)
        adapted["model"] = profile.model_id

        if not profile.supports_tool_calling and "tools" in adapted:
            tools = adapted.pop("tools", [])
            tool_description = self._tools_to_text(tools)
            messages = list(adapted.get("messages", []))
            if messages and tool_description:
                messages[0] = dict(messages[0])
                messages[0]["content"] = (
                    tool_description + "\n\n" + messages[0].get("content", "")
                )
            adapted["messages"] = messages

        if not profile.supports_json_mode:
            adapted.pop("response_format", None)

        return adapted

    def _tools_to_text(self, tools: List[Dict]) -> str:
        if not tools:
            return ""
        lines = ["Available tools (respond with JSON to call one):"]
        for tool in tools:
            fn = tool.get("function", tool)
            lines.append(f"- {fn.get('name', '')}: {fn.get('description', '')}")
        return "\n".join(lines)
```

## Solution 5: Model Switch Event Log

```python
import time
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ModelSwitchEvent:
    from_model: Optional[str]
    to_model: str
    reason: str
    required_capabilities: List[str]
    switched_at: float = field(default_factory=time.time)


class ModelSwitchEventLog:
    """Records model switch events for observability and post-incident analysis."""

    def __init__(self, max_events: int = 1000):
        self._events: List[ModelSwitchEvent] = []
        self._max = max_events

    def record(
        self,
        from_model: Optional[str],
        to_model: str,
        reason: str,
        required_capabilities: List[str] = None,
    ) -> None:
        if len(self._events) >= self._max:
            self._events.pop(0)
        self._events.append(ModelSwitchEvent(
            from_model=from_model,
            to_model=to_model,
            reason=reason,
            required_capabilities=required_capabilities or [],
        ))

    def recent(self, window_seconds: float = 3600.0) -> List[ModelSwitchEvent]:
        cutoff = time.time() - window_seconds
        return [e for e in self._events if e.switched_at >= cutoff]

    def summary(self, window_seconds: float = 3600.0) -> dict:
        recent = self.recent(window_seconds)
        return {
            "switch_count": len(recent),
            "models_used": list({e.to_model for e in recent}),
        }
```

## Solution 6: Fallback Router Dashboard

```python
import time


class FallbackModelRouterDashboard:
    """Combines router stats, model health, and switch history."""

    def __init__(
        self,
        router: FallbackModelRouter,
        route_table: ModelRouteTable,
        switch_log: ModelSwitchEventLog,
    ):
        self._router = router
        self._table = route_table
        self._log = switch_log

    def render(self) -> dict:
        router_stats = self._router.stats()
        profiles = self._table.all_profiles()
        switch_summary = self._log.summary(3600)

        return {
            "generated_at": time.time(),
            "active_model": router_stats["active_model"],
            "model_switches_total": router_stats["model_switches"],
            "failed_models": router_stats["failed_models"],
            "models": [
                {
                    "model_id": p.model_id,
                    "provider": p.provider,
                    "tier": p.tier,
                    "failed": self._table.is_failed(p.model_id),
                    "tool_calling": p.supports_tool_calling,
                }
                for p in profiles
            ],
            "switch_events_1h": switch_summary["switch_count"],
            "providers_in_use_1h": switch_summary["models_used"],
        }
```

## Comparison

| Approach | Capability Matching | Auto-Failover | Recovery Probe | Request Adaptation | Dashboard |
|---|---|---|---|---|---|
| ModelRouteTable | Yes | No | No | No | No |
| FallbackModelRouter | Via route table | Yes | Yes (periodic) | No | No |
| CapabilityAwareRequestBuilder | Via profile | No | No | Yes | No |
| ModelSwitchEventLog | No | No | No | No | No |
| FallbackModelRouterDashboard | No | No | No | No | Yes |

**Best for production**: Configure at least two providers in the route table — primary (fastest, cheapest) and secondary (different provider). Set `failure_window_seconds=60` so failed models are retried after one minute, which is appropriate for most transient provider outages. Use `CapabilityAwareRequestBuilder` to degrade gracefully when the fallback lacks tool-calling support: text-based tool invocation is slower but prevents a hard failure. Monitor `switch_events_1h > 5` as a signal that the primary provider is having sustained trouble — consider flipping the primary/secondary order during a prolonged incident.
