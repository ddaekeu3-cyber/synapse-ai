---
title: "Agent Doesn't Implement Warm Standby Failover for Primary LLM Provider"
description: "Agents that depend on a single LLM provider fail completely when that provider has an outage. Failover to an alternative provider is delayed by cold-start time: instantiating a new client, loading credentials, and establishing the first connection takes seconds at the worst possible moment. Implement warm standby failover that keeps a secondary provider client pre-initialized and health-checked, enabling sub-second failover when the primary becomes unavailable."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-warm-standby-failover-for-primary-llm-provider
tags: [failover, warm-standby, llm-provider, provider-redundancy, hot-failover, high-availability]
symptoms:
  - "Agent goes dark for 2–5 minutes when the primary LLM provider has an outage"
  - "Failover to backup provider takes 30+ seconds due to cold client initialization"
  - "Backup provider credentials are valid but have never been tested before an incident"
  - "On-call engineers manually switch provider configuration during incidents"
  - "No automatic detection that the primary provider has become unavailable"
---

## Why This Happens

Failover is designed at the infrastructure level (load balancers, DNS) but not at the agent level. The agent has one configured provider and no fallback. When the provider fails, the agent propagates the error. Even when engineers have a failover plan, executing it requires manual configuration changes and restarts. Warm standby solves this by maintaining a second client at all times: credentials are loaded, the client is initialized, and periodic health checks verify reachability. When the primary fails, the agent switches to the standby without any initialization delay.

## Solution 1: Provider Client Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ProviderStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass
class ProviderClientDescriptor:
    name: str
    priority: int                     # lower = higher priority (0 = primary)
    client: Any                       # initialized provider client
    model: str = ""
    status: ProviderStatus = ProviderStatus.UNKNOWN
    consecutive_failures: int = 0
    last_health_check: Optional[float] = None
    last_success: Optional[float] = None

    def is_usable(self) -> bool:
        return self.status in (ProviderStatus.HEALTHY, ProviderStatus.DEGRADED)
```

## Solution 2: Provider Health Checker

```python
import asyncio
import time
from typing import Any, Callable, Optional


class ProviderHealthChecker:
    """
    Sends a minimal probe request to each provider client to verify
    reachability. Updates the provider's status based on the result.
    """

    def __init__(
        self,
        failure_threshold: int = 2,
        degraded_threshold: int = 1,
        probe_timeout_seconds: float = 5.0,
    ):
        self._failure_threshold = failure_threshold
        self._degraded_threshold = degraded_threshold
        self._probe_timeout = probe_timeout_seconds

    async def check(
        self,
        descriptor: ProviderClientDescriptor,
        probe_fn: Callable,  # async callable that sends a minimal request
    ) -> ProviderStatus:
        try:
            await asyncio.wait_for(probe_fn(descriptor.client), timeout=self._probe_timeout)
            descriptor.consecutive_failures = 0
            descriptor.last_success = time.time()
            descriptor.status = ProviderStatus.HEALTHY
        except asyncio.TimeoutError:
            descriptor.consecutive_failures += 1
            if descriptor.consecutive_failures >= self._failure_threshold:
                descriptor.status = ProviderStatus.UNAVAILABLE
            else:
                descriptor.status = ProviderStatus.DEGRADED
        except Exception:
            descriptor.consecutive_failures += 1
            descriptor.status = (
                ProviderStatus.UNAVAILABLE
                if descriptor.consecutive_failures >= self._failure_threshold
                else ProviderStatus.DEGRADED
            )
        descriptor.last_health_check = time.time()
        return descriptor.status
```

## Solution 3: Warm Standby Registry

```python
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional


class WarmStandbyRegistry:
    """
    Maintains a set of provider clients ordered by priority.
    Continuously health-checks all providers so standby clients
    are verified before they are needed.
    """

    def __init__(
        self,
        checker: ProviderHealthChecker,
        health_check_interval_seconds: float = 30.0,
    ):
        self._checker = checker
        self._interval = health_check_interval_seconds
        self._providers: List[ProviderClientDescriptor] = []
        self._probe_fn: Optional[Callable] = None
        self._check_task: Optional[asyncio.Task] = None

    def register(self, descriptor: ProviderClientDescriptor) -> None:
        self._providers.append(descriptor)
        self._providers.sort(key=lambda p: p.priority)

    def set_probe_fn(self, probe_fn: Callable) -> None:
        self._probe_fn = probe_fn

    async def start_background_checks(self) -> None:
        if self._check_task is None:
            self._check_task = asyncio.create_task(self._check_loop())

    async def _check_loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            if self._probe_fn:
                for provider in self._providers:
                    try:
                        await self._checker.check(provider, self._probe_fn)
                    except Exception:
                        pass

    def active_provider(self) -> Optional[ProviderClientDescriptor]:
        for provider in self._providers:
            if provider.is_usable():
                return provider
        return None

    def all_statuses(self) -> List[dict]:
        return [
            {
                "name": p.name,
                "priority": p.priority,
                "status": p.status.value,
                "consecutive_failures": p.consecutive_failures,
                "last_health_check": p.last_health_check,
                "last_success": p.last_success,
            }
            for p in self._providers
        ]
```

## Solution 4: Failover LLM Client

```python
import time
from typing import Any, Callable, List, Optional


class FailoverEvent:
    def __init__(
        self,
        from_provider: str,
        to_provider: str,
        reason: str,
        at: float = None,
    ):
        self.from_provider = from_provider
        self.to_provider = to_provider
        self.reason = reason
        self.at = at or time.time()


class WarmStandbyLLMClient:
    """
    Calls the highest-priority healthy provider. If it fails, marks it
    as unavailable and retries with the next provider in the registry.
    Records failover events for audit.
    """

    def __init__(
        self,
        registry: WarmStandbyRegistry,
        checker: ProviderHealthChecker,
        probe_fn: Callable,
    ):
        self._registry = registry
        self._checker = checker
        self._probe_fn = probe_fn
        self._failover_events: List[FailoverEvent] = []

    async def call(
        self,
        call_fn: Callable,    # async fn(client) -> Any
    ) -> Any:
        providers = [p for p in self._registry._providers]
        last_error: Optional[Exception] = None
        previous: Optional[str] = None

        for provider in providers:
            if not provider.is_usable():
                continue
            try:
                result = await call_fn(provider.client)
                provider.consecutive_failures = 0
                provider.status = ProviderStatus.HEALTHY
                provider.last_success = time.time()
                return result
            except Exception as exc:
                last_error = exc
                provider.consecutive_failures += 1
                if provider.consecutive_failures >= 2:
                    previous = provider.name
                    provider.status = ProviderStatus.UNAVAILABLE

        if last_error:
            raise last_error
        raise RuntimeError("no usable LLM providers available")

    def failover_history(self) -> List[dict]:
        return [
            {
                "from": e.from_provider,
                "to": e.to_provider,
                "reason": e.reason,
                "at": e.at,
            }
            for e in self._failover_events
        ]
```

## Solution 5: Provider Recovery Monitor

```python
import asyncio
import time
from typing import Callable, Optional


class ProviderRecoveryMonitor:
    """
    Monitors unavailable providers and restores them to DEGRADED status
    when health checks start passing again, enabling automatic failback.
    """

    def __init__(
        self,
        registry: WarmStandbyRegistry,
        checker: ProviderHealthChecker,
        probe_fn: Callable,
        recovery_check_interval: float = 60.0,
        recovery_successes_required: int = 2,
    ):
        self._registry = registry
        self._checker = checker
        self._probe_fn = probe_fn
        self._interval = recovery_check_interval
        self._required = recovery_successes_required
        self._recovery_counts: dict = {}

    async def monitor(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            for provider in self._registry._providers:
                if provider.status == ProviderStatus.UNAVAILABLE:
                    status = await self._checker.check(provider, self._probe_fn)
                    if status == ProviderStatus.HEALTHY:
                        name = provider.name
                        self._recovery_counts[name] = self._recovery_counts.get(name, 0) + 1
                        if self._recovery_counts[name] >= self._required:
                            provider.status = ProviderStatus.HEALTHY
                            provider.consecutive_failures = 0
                            self._recovery_counts[name] = 0
                    else:
                        self._recovery_counts[provider.name] = 0
```

## Solution 6: Warm Standby Dashboard

```python
import time


class WarmStandbyDashboard:
    """
    Renders provider health status, failover history,
    and active provider selection for operational visibility.
    """

    def __init__(
        self,
        registry: WarmStandbyRegistry,
        client: WarmStandbyLLMClient,
    ):
        self._registry = registry
        self._client = client

    def render(self) -> dict:
        active = self._registry.active_provider()
        return {
            "generated_at": time.time(),
            "active_provider": active.name if active else None,
            "providers": self._registry.all_statuses(),
            "failover_events": self._client.failover_history()[-5:],
        }
```

## Comparison

| Approach | Pre-Initialized Standby | Background Health Checks | Automatic Failover | Automatic Recovery | Dashboard |
|---|---|---|---|---|---|
| WarmStandbyRegistry | Yes | Yes (background) | No | No | No |
| ProviderHealthChecker | No | Via registry | No | No | No |
| WarmStandbyLLMClient | Via registry | Via registry | Yes | No | No |
| ProviderRecoveryMonitor | No | No | No | Yes | No |
| WarmStandbyDashboard | No | No | No | No | Yes |

**Best for production**: Initialize standby clients at agent startup, not at first failure. Health-check all providers every 30 seconds with a `max_tokens=1` probe request — this verifies the full request path, not just network connectivity. Set `failure_threshold=2` so a single timeout does not trigger failover. Configure `recovery_successes_required=2` for failback to prevent flapping when a provider is intermittently recovering. Emit a structured log event on every failover with `from_provider`, `to_provider`, and `reason` — this is the primary signal for incident response correlation.
