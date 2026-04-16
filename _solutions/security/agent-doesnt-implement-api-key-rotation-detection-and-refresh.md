---
title: "Agent Doesn't Implement API Key Rotation Detection and Refresh"
description: "Agents that load API keys at startup and cache them indefinitely fail when keys are rotated — they continue sending the old key until they receive 401 errors, then fail all requests until manually restarted. Implement API key rotation detection that monitors 401 responses, triggers automatic key refresh from a secrets backend, retries the failed request with the new key, and avoids thundering-herd refresh races when multiple concurrent requests detect the rotation simultaneously."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-api-key-rotation-detection-and-refresh
tags: [api-key-rotation, secret-refresh, credential-rotation, 401-handling, secrets-management, zero-downtime]
symptoms:
  - "Agent returns 401 errors for all requests after the API key was rotated in the secrets store"
  - "Process restart required every time keys are rotated — downtime during each rotation"
  - "Multiple concurrent sessions all hit 401 simultaneously and all attempt to refresh the key at once"
  - "No mechanism to detect that a cached key is stale vs. a legitimate authentication failure"
  - "Key refresh succeeds but in-flight requests that used the old key are not retried"
---

## Why This Happens

API keys are loaded once at startup and stored in memory. When the key is rotated in the secrets manager, the agent continues using the cached value until it receives a 401, at which point all requests fail until the process is restarted. Key rotation handling requires detecting the 401 as a potential rotation signal, fetching the new key from the secrets backend, and retrying the original request — all while ensuring that 100 concurrent sessions do not each trigger an independent refresh, which would hammer the secrets API and cause a race condition.

## Solution 1: Key State Record

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class KeyState:
    key_name: str
    current_value: Optional[bytes] = None
    loaded_at: Optional[float] = None
    last_401_at: Optional[float] = None
    refresh_count: int = 0
    consecutive_401s: int = 0

    def age_seconds(self) -> Optional[float]:
        if self.loaded_at is None:
            return None
        return time.time() - self.loaded_at

    def is_loaded(self) -> bool:
        return self.current_value is not None

    def mark_401(self) -> None:
        self.last_401_at = time.time()
        self.consecutive_401s += 1

    def mark_refreshed(self, new_value: bytes) -> None:
        self.current_value = new_value
        self.loaded_at = time.time()
        self.refresh_count += 1
        self.consecutive_401s = 0
```

## Solution 2: Secrets Backend Interface

```python
import os
from typing import Optional


class SecretsBackend:
    """
    Abstract interface for fetching secrets.
    Implementations: environment variable, AWS Secrets Manager, HashiCorp Vault, etc.
    """

    async def fetch(self, key_name: str) -> Optional[bytes]:
        raise NotImplementedError


class EnvironmentSecretsBackend(SecretsBackend):
    """Reads secrets from environment variables. Useful for local dev and testing."""

    async def fetch(self, key_name: str) -> Optional[bytes]:
        value = os.environ.get(key_name)
        if value is None:
            return None
        return value.encode("utf-8")


class CachingSecretsBackend(SecretsBackend):
    """
    Wraps another backend with a TTL cache to avoid hammering the secrets API.
    Forces a fresh fetch when force_refresh=True.
    """

    def __init__(
        self,
        backend: SecretsBackend,
        cache_ttl_seconds: float = 300.0,
    ):
        self._backend = backend
        self._ttl = cache_ttl_seconds
        self._cache: dict = {}
        self._timestamps: dict = {}

    async def fetch(
        self,
        key_name: str,
        force_refresh: bool = False,
    ) -> Optional[bytes]:
        import time
        if not force_refresh:
            ts = self._timestamps.get(key_name, 0)
            if time.time() - ts < self._ttl and key_name in self._cache:
                return self._cache[key_name]
        value = await self._backend.fetch(key_name)
        if value is not None:
            self._cache[key_name] = value
            self._timestamps[key_name] = time.time()
        return value
```

## Solution 3: Key Rotation Detector

```python
import asyncio
import time
from typing import Dict, Optional


class ApiKeyRotationDetector:
    """
    Detects API key rotation via 401 responses and triggers a refresh.
    Uses an asyncio.Lock per key to serialize refreshes and prevent
    thundering-herd when multiple concurrent requests detect the rotation.
    """

    MAX_CONSECUTIVE_401S = 3   # treat as hard auth failure after this many

    def __init__(
        self,
        backend: CachingSecretsBackend,
        max_refresh_attempts: int = 2,
    ):
        self._backend = backend
        self._max_refresh = max_refresh_attempts
        self._states: Dict[str, KeyState] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def _state(self, key_name: str) -> KeyState:
        if key_name not in self._states:
            self._states[key_name] = KeyState(key_name=key_name)
            self._locks[key_name] = asyncio.Lock()
        return self._states[key_name]

    async def get_key(self, key_name: str) -> Optional[bytes]:
        state = self._state(key_name)
        if not state.is_loaded():
            await self._refresh(key_name)
        return state.current_value

    async def handle_401(self, key_name: str) -> Optional[bytes]:
        """
        Called when a 401 is received using key_name.
        Refreshes the key once and returns the new value.
        Returns None if refresh fails or consecutive 401s exceed max.
        """
        state = self._state(key_name)
        state.mark_401()

        if state.consecutive_401s > self.MAX_CONSECUTIVE_401S:
            return None  # persistent auth failure — not a rotation

        lock = self._locks[key_name]
        async with lock:
            # Check if another coroutine already refreshed while we waited
            if state.refresh_count > 0 and state.consecutive_401s == 0:
                return state.current_value

            for _ in range(self._max_refresh):
                new_value = await self._backend.fetch(key_name, force_refresh=True)
                if new_value and new_value != state.current_value:
                    state.mark_refreshed(new_value)
                    return new_value
                await asyncio.sleep(1.0)

        return None

    async def _refresh(self, key_name: str) -> None:
        state = self._state(key_name)
        value = await self._backend.fetch(key_name)
        if value:
            state.mark_refreshed(value)

    def stats(self) -> dict:
        return {
            key: {
                "refresh_count": s.refresh_count,
                "consecutive_401s": s.consecutive_401s,
                "age_seconds": s.age_seconds(),
            }
            for key, s in self._states.items()
        }
```

## Solution 4: Rotation-Aware HTTP Client Wrapper

```python
import asyncio
from typing import Any, Callable, Optional


class RotationAwareClientWrapper:
    """
    Wraps an HTTP client call with API key rotation handling.
    On 401, triggers a key refresh and retries the original request once.
    """

    def __init__(
        self,
        detector: ApiKeyRotationDetector,
        key_name: str,
    ):
        self._detector = detector
        self._key_name = key_name

    async def call(
        self,
        request_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        """
        request_fn must accept an `api_key` kwarg.
        On 401, refreshes the key and retries once.
        """
        key = await self._detector.get_key(self._key_name)
        if key is None:
            raise ValueError(f"Key '{self._key_name}' not available")

        try:
            return await request_fn(*args, api_key=key.decode(), **kwargs)
        except Exception as exc:
            if not self._is_401(exc):
                raise

            new_key = await self._detector.handle_401(self._key_name)
            if new_key is None:
                raise ValueError(
                    f"Authentication failed for '{self._key_name}' and key refresh failed"
                ) from exc

            return await request_fn(*args, api_key=new_key.decode(), **kwargs)

    def _is_401(self, exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
        return status == 401
```

## Solution 5: Proactive Key Pre-Rotation

```python
import asyncio
import time
from typing import Optional


class ProactiveKeyPreRotator:
    """
    Pre-fetches keys before they expire based on a known rotation schedule.
    Reduces 401s by refreshing ahead of the rotation window.
    """

    def __init__(
        self,
        detector: ApiKeyRotationDetector,
        key_names: list,
        refresh_before_expiry_seconds: float = 300.0,
        key_ttl_seconds: float = 3600.0,
        poll_interval_seconds: float = 60.0,
    ):
        self._detector = detector
        self._key_names = key_names
        self._refresh_before = refresh_before_expiry_seconds
        self._key_ttl = key_ttl_seconds
        self._poll_interval = poll_interval_seconds
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._pre_rotation_loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _pre_rotation_loop(self) -> None:
        while True:
            await asyncio.sleep(self._poll_interval)
            for key_name in self._key_names:
                state = self._detector._state(key_name)
                age = state.age_seconds()
                if age is not None and age >= (self._key_ttl - self._refresh_before):
                    await self._detector._refresh(key_name)
```

## Solution 6: Key Rotation Audit Log

```python
import time
from dataclasses import dataclass, field
from typing import List


@dataclass
class KeyRotationEvent:
    key_name: str
    event_type: str   # "loaded" | "refreshed" | "401_detected" | "refresh_failed"
    triggered_by: str = "auto"
    timestamp: float = field(default_factory=time.time)


class KeyRotationAuditLog:
    """Tracks key rotation events for compliance and incident analysis."""

    def __init__(self, max_events: int = 1000):
        self._events: List[KeyRotationEvent] = []
        self._max = max_events

    def record(self, key_name: str, event_type: str, triggered_by: str = "auto") -> None:
        if len(self._events) >= self._max:
            self._events.pop(0)
        self._events.append(KeyRotationEvent(
            key_name=key_name,
            event_type=event_type,
            triggered_by=triggered_by,
        ))

    def recent(self, window_seconds: float = 86400.0) -> List[KeyRotationEvent]:
        cutoff = time.time() - window_seconds
        return [e for e in self._events if e.timestamp >= cutoff]

    def summary(self) -> dict:
        recent = self.recent(86400)
        return {
            "events_24h": len(recent),
            "refreshes": sum(1 for e in recent if e.event_type == "refreshed"),
            "401s_detected": sum(1 for e in recent if e.event_type == "401_detected"),
            "refresh_failures": sum(1 for e in recent if e.event_type == "refresh_failed"),
        }
```

## Comparison

| Approach | 401 Detection | Refresh Serialization | Proactive Rotation | Retry on New Key | Audit Log |
|---|---|---|---|---|---|
| ApiKeyRotationDetector | Yes | Yes (per-key lock) | No | No | No |
| RotationAwareClientWrapper | Via detector | Via detector | No | Yes (one retry) | No |
| ProactiveKeyPreRotator | No | Via detector | Yes (scheduled) | No | No |
| KeyRotationAuditLog | No | No | No | No | Yes |

**Best for production**: Combine `RotationAwareClientWrapper` (reactive — handles unexpected rotations) with `ProactiveKeyPreRotator` (proactive — pre-fetches before expiry). The combination means 401s from rotation become extremely rare: the pre-rotator refreshes before expiry, and the wrapper handles the rare case where rotation happened outside the schedule. Use `CachingSecretsBackend` with `cache_ttl_seconds=300` to prevent hitting the secrets API on every request while still ensuring keys are fresh. Log every `401_detected` and `refreshed` event — a sudden spike in 401s that are not followed by successful refreshes indicates a configuration error or revoked credentials requiring human intervention.
