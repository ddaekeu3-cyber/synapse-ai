---
title: "Agent Doesn't Implement Circuit Breaker State Persistence Across Restarts"
description: "Agents that hold circuit breaker state only in memory reset every breaker to CLOSED on restart — so a service that was failing before restart immediately receives a flood of requests the moment the agent comes back up. Implement circuit breaker state persistence that saves open/half-open state and failure counts to durable storage and restores them on startup, preventing restart-triggered overload on already-degraded dependencies."
date: 2026-04-16
difficulty: advanced
category: reliability
slug: agent-doesnt-implement-circuit-breaker-state-persistence-across-restarts
tags: [circuit-breaker, state-persistence, restart-resilience, fault-tolerance, overload-protection, durability]
symptoms:
  - "Every agent restart resets all circuit breakers to CLOSED, flooding degraded dependencies"
  - "Circuit breaker opens due to repeated failures, agent restarts, breaker resets — infinite loop"
  - "No record of which services were failing at time of restart"
  - "Post-deployment restarts during an incident make the incident worse"
  - "Circuit breaker metrics disappear on restart, breaking continuity of on-call dashboards"
---

## Why This Happens

Circuit breakers stored as in-process objects are ephemeral. A process restart — triggered by a deployment, crash, OOM kill, or scheduled rotation — returns every breaker to CLOSED with zero failure count. If a downstream service was failing before the restart, the agent immediately starts making requests again, re-accumulates failures, and reopens the breaker — wasting the recovery time and potentially contributing to a thundering herd. Persisting breaker state requires serializing the state enum and failure window to a durable store (Redis, database, or a local file) and reading it back at startup before the first request is dispatched.

## Solution 1: Persistent Circuit Breaker State

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class PersistedBreakerState:
    service_name: str
    state: BreakerState
    failure_count: int
    success_count: int
    last_failure_at: Optional[float]
    opened_at: Optional[float]
    half_opened_at: Optional[float]
    updated_at: float = field(default_factory=time.time)
    failure_timestamps: List[float] = field(default_factory=list)

    def is_stale(self, max_age_seconds: float = 300.0) -> bool:
        return time.time() - self.updated_at > max_age_seconds
```

## Solution 2: Circuit Breaker State Store

```python
import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Dict, Optional


class CircuitBreakerStateStore:
    """
    Persists circuit breaker states to a local JSON file.
    Replace with a Redis or database backend for multi-instance deployments.
    """

    def __init__(self, path: str = "/tmp/circuit_breaker_state.json"):
        self._path = Path(path)
        self._lock = Lock()

    def save(self, state: PersistedBreakerState) -> None:
        with self._lock:
            existing = self._load_all()
            existing[state.service_name] = {
                "state": state.state.value,
                "failure_count": state.failure_count,
                "success_count": state.success_count,
                "last_failure_at": state.last_failure_at,
                "opened_at": state.opened_at,
                "half_opened_at": state.half_opened_at,
                "updated_at": state.updated_at,
                "failure_timestamps": state.failure_timestamps[-50:],  # keep last 50
            }
            self._path.write_text(json.dumps(existing, indent=2))

    def load(self, service_name: str) -> Optional[PersistedBreakerState]:
        with self._lock:
            all_states = self._load_all()
            data = all_states.get(service_name)
            if not data:
                return None
            return PersistedBreakerState(
                service_name=service_name,
                state=BreakerState(data["state"]),
                failure_count=data["failure_count"],
                success_count=data["success_count"],
                last_failure_at=data.get("last_failure_at"),
                opened_at=data.get("opened_at"),
                half_opened_at=data.get("half_opened_at"),
                updated_at=data.get("updated_at", time.time()),
                failure_timestamps=data.get("failure_timestamps", []),
            )

    def _load_all(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}

    def delete(self, service_name: str) -> None:
        with self._lock:
            existing = self._load_all()
            existing.pop(service_name, None)
            self._path.write_text(json.dumps(existing, indent=2))

    def all_states(self) -> Dict[str, dict]:
        with self._lock:
            return self._load_all()
```

## Solution 3: Persistent Circuit Breaker

```python
import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 5
    success_threshold: int = 2       # successes needed in HALF_OPEN to close
    open_duration_seconds: float = 60.0
    window_seconds: float = 120.0    # sliding window for failure counting
    stale_state_max_age_seconds: float = 300.0


class PersistentCircuitBreaker:
    """
    Circuit breaker with durable state. Loads persisted state on construction,
    saves state on every transition. Stale persisted states (from an old instance
    that crashed long ago) are ignored and the breaker starts fresh.
    """

    def __init__(
        self,
        service_name: str,
        config: CircuitBreakerConfig,
        store: CircuitBreakerStateStore,
    ):
        self._name = service_name
        self._config = config
        self._store = store
        self._lock = asyncio.Lock()
        self._state = self._restore_or_initialize()

    def _restore_or_initialize(self) -> PersistedBreakerState:
        persisted = self._store.load(self._name)
        if persisted and not persisted.is_stale(self._config.stale_state_max_age_seconds):
            return persisted
        return PersistedBreakerState(
            service_name=self._name,
            state=BreakerState.CLOSED,
            failure_count=0,
            success_count=0,
            last_failure_at=None,
            opened_at=None,
            half_opened_at=None,
        )

    async def call(self, fn: Callable, *args: Any, **kwargs: Any) -> Any:
        async with self._lock:
            self._maybe_transition()
            if self._state.state == BreakerState.OPEN:
                raise CircuitOpenError(self._name)

        try:
            result = await fn(*args, **kwargs)
            async with self._lock:
                await self._on_success()
            return result
        except Exception as exc:
            async with self._lock:
                await self._on_failure()
            raise

    def _maybe_transition(self) -> None:
        if (
            self._state.state == BreakerState.OPEN
            and self._state.opened_at is not None
            and time.time() - self._state.opened_at >= self._config.open_duration_seconds
        ):
            self._state.state = BreakerState.HALF_OPEN
            self._state.half_opened_at = time.time()
            self._state.success_count = 0
            self._save()

    async def _on_success(self) -> None:
        if self._state.state == BreakerState.HALF_OPEN:
            self._state.success_count += 1
            if self._state.success_count >= self._config.success_threshold:
                self._state.state = BreakerState.CLOSED
                self._state.failure_count = 0
                self._state.failure_timestamps = []
                self._save()
        elif self._state.state == BreakerState.CLOSED:
            pass  # no state change needed

    async def _on_failure(self) -> None:
        now = time.time()
        self._state.failure_timestamps.append(now)
        cutoff = now - self._config.window_seconds
        self._state.failure_timestamps = [
            t for t in self._state.failure_timestamps if t >= cutoff
        ]
        self._state.failure_count = len(self._state.failure_timestamps)
        self._state.last_failure_at = now

        if self._state.failure_count >= self._config.failure_threshold:
            if self._state.state != BreakerState.OPEN:
                self._state.state = BreakerState.OPEN
                self._state.opened_at = now
                self._save()
        else:
            self._save()

    def _save(self) -> None:
        self._state.updated_at = time.time()
        self._store.save(self._state)

    def current_state(self) -> BreakerState:
        return self._state.state


class CircuitOpenError(Exception):
    def __init__(self, service_name: str):
        super().__init__(f"circuit breaker OPEN for service '{service_name}'")
        self.service_name = service_name
```

## Solution 4: Multi-Service Breaker Registry

```python
from typing import Dict, Optional


class PersistentBreakerRegistry:
    """
    Manages a set of named circuit breakers, each with persisted state.
    Breakers are created lazily on first access with the default config,
    or with a per-service override.
    """

    def __init__(
        self,
        store: CircuitBreakerStateStore,
        default_config: Optional[CircuitBreakerConfig] = None,
    ):
        self._store = store
        self._default_config = default_config or CircuitBreakerConfig()
        self._breakers: Dict[str, PersistentCircuitBreaker] = {}
        self._configs: Dict[str, CircuitBreakerConfig] = {}

    def register(
        self,
        service_name: str,
        config: Optional[CircuitBreakerConfig] = None,
    ) -> None:
        self._configs[service_name] = config or self._default_config

    def get(self, service_name: str) -> PersistentCircuitBreaker:
        if service_name not in self._breakers:
            config = self._configs.get(service_name, self._default_config)
            self._breakers[service_name] = PersistentCircuitBreaker(
                service_name=service_name,
                config=config,
                store=self._store,
            )
        return self._breakers[service_name]

    def all_states(self) -> dict:
        return {
            name: breaker.current_state().value
            for name, breaker in self._breakers.items()
        }
```

## Solution 5: Startup State Reviewer

```python
import time
from typing import List


class StartupStateReviewer:
    """
    Runs at agent startup and reports which circuit breakers were
    open or half-open at the time of the previous shutdown/crash.
    Allows operators to decide whether to manually reset or honor the state.
    """

    def __init__(self, store: CircuitBreakerStateStore):
        self._store = store

    def review(self) -> List[dict]:
        all_states = self._store.all_states()
        flagged = []
        for service_name, data in all_states.items():
            state = data.get("state", "closed")
            if state in ("open", "half_open"):
                age_seconds = time.time() - data.get("updated_at", time.time())
                flagged.append({
                    "service_name": service_name,
                    "state": state,
                    "failure_count": data.get("failure_count", 0),
                    "opened_at": data.get("opened_at"),
                    "state_age_seconds": round(age_seconds, 1),
                    "recommendation": (
                        "honor — service was recently failing"
                        if age_seconds < 300
                        else "consider reset — state is stale"
                    ),
                })
        return flagged
```

## Solution 6: Circuit Breaker Persistence Dashboard

```python
import time


class CircuitBreakerPersistenceDashboard:
    """
    Combines live breaker states, persisted states, and startup review
    into a single snapshot for on-call visibility.
    """

    def __init__(
        self,
        registry: PersistentBreakerRegistry,
        store: CircuitBreakerStateStore,
        reviewer: StartupStateReviewer,
    ):
        self._registry = registry
        self._store = store
        self._reviewer = reviewer

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "live_states": self._registry.all_states(),
            "persisted_states": self._store.all_states(),
            "startup_flagged": self._reviewer.review(),
        }
```

## Comparison

| Approach | In-Memory State | Persisted State | Startup Restore | Multi-Service | Startup Review |
|---|---|---|---|---|---|
| PersistentCircuitBreaker | Yes | Yes (file/Redis) | Yes | No | No |
| CircuitBreakerStateStore | No | Yes | Via load() | Yes | No |
| PersistentBreakerRegistry | Via breakers | Via store | Via breakers | Yes | No |
| StartupStateReviewer | No | Via store | No | Yes | Yes |
| CircuitBreakerPersistenceDashboard | No | No | No | No | Yes |

**Best for production**: Use Redis with a TTL equal to `open_duration_seconds * 3` as the state backend in multi-instance deployments — all instances share a single breaker state, preventing the scenario where one instance opens its breaker while another (with a fresh restart) floods the degraded service. At startup, run `StartupStateReviewer.review()` and emit the results as a structured log event: a deployment during an active incident will show open breakers that operators may want to preserve. Set `stale_state_max_age_seconds=300` so a breaker that was open five minutes ago (likely already recovered) does not block traffic unnecessarily after a scheduled restart.
