---
title: "Agent Doesn't Implement LLM API Key Rotation Without Downtime"
description: "Agents that hard-code or singleton-load LLM API keys require a restart to rotate credentials — creating a downtime window during key rotation and delaying response to a leaked key. Implement zero-downtime API key rotation with a live-reloadable credential store, graceful draining of in-flight requests using the old key, and automatic switchover when a new key is detected."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-llm-api-key-rotation-without-downtime
tags: [api-key-rotation, zero-downtime, credential-management, key-lifecycle, live-reload, secret-rotation]
symptoms:
  - "API key rotation requires restarting the agent process, causing downtime"
  - "Leaked key cannot be revoked quickly because rotation takes 5+ minutes"
  - "In-flight LLM requests fail when the key is rotated mid-execution"
  - "Agent reads the API key once at startup and never checks for updates"
  - "No signal to the agent that a credential has been rotated in the secrets manager"
---

## Why This Happens

API keys are typically read from environment variables or a config file at process startup and stored in a singleton client object. Rotating the key requires updating the environment variable or config file and restarting the process for the new value to take effect. In zero-downtime environments, this means either accepting a restart window or running overlapping processes during rotation. Live reload requires the agent to periodically check for credential updates, track which key is currently in use, and switch new requests to the new key while allowing existing requests to complete with the old key.

## Solution 1: Rotatable Credential

```python
import hashlib
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RotatableCredential:
    key_id: str              # stable identifier for this key (not the secret itself)
    api_key: str             # the actual secret — never logged
    provider: str
    created_at: float = field(default_factory=time.time)
    activated_at: Optional[float] = None
    revoked_at: Optional[float] = None
    in_flight_count: int = 0

    def fingerprint(self) -> str:
        return hashlib.sha256(self.api_key.encode()).hexdigest()[:8]

    def is_active(self) -> bool:
        return self.revoked_at is None

    def can_drain(self) -> bool:
        return self.revoked_at is not None and self.in_flight_count == 0
```

## Solution 2: Live Credential Loader

```python
import os
import time
from typing import Callable, Optional


class LiveCredentialLoader:
    """
    Reads the current API key from a configured source (env var, file, or
    secrets manager callback) and detects when it has changed.
    """

    def __init__(
        self,
        source_fn: Callable[[], str],
        check_interval_seconds: float = 30.0,
    ):
        self._source_fn = source_fn
        self._interval = check_interval_seconds
        self._last_checked = 0.0
        self._cached_key: Optional[str] = None

    @staticmethod
    def from_env(var_name: str, **kwargs) -> "LiveCredentialLoader":
        return LiveCredentialLoader(
            source_fn=lambda: os.environ.get(var_name, ""),
            **kwargs,
        )

    @staticmethod
    def from_file(file_path: str, **kwargs) -> "LiveCredentialLoader":
        def read_file():
            try:
                with open(file_path) as f:
                    return f.read().strip()
            except OSError:
                return ""
        return LiveCredentialLoader(source_fn=read_file, **kwargs)

    def current_key(self) -> str:
        now = time.time()
        if now - self._last_checked >= self._interval or self._cached_key is None:
            self._cached_key = self._source_fn()
            self._last_checked = now
        return self._cached_key

    def has_changed(self) -> bool:
        new_key = self._source_fn()
        changed = new_key != self._cached_key
        if changed:
            self._cached_key = new_key
            self._last_checked = time.time()
        return changed
```

## Solution 3: Zero-Downtime Key Rotator

```python
import asyncio
import time
from threading import Lock
from typing import Dict, Optional


class ZeroDowntimeKeyRotator:
    """
    Manages the lifecycle of API key rotation without downtime.
    New requests always use the newest active key.
    Old keys are kept alive until all in-flight requests drain.
    """

    def __init__(
        self,
        loader: LiveCredentialLoader,
        provider: str,
        drain_timeout_seconds: float = 60.0,
    ):
        self._loader = loader
        self._provider = provider
        self._drain_timeout = drain_timeout_seconds
        self._credentials: Dict[str, RotatableCredential] = {}
        self._active_key_id: Optional[str] = None
        self._lock = Lock()
        self._rotation_count = 0

    def _make_key_id(self, api_key: str) -> str:
        import hashlib
        return hashlib.sha256(api_key.encode()).hexdigest()[:12]

    def current_key(self) -> str:
        raw_key = self._loader.current_key()
        key_id = self._make_key_id(raw_key)

        with self._lock:
            if key_id != self._active_key_id:
                # New key detected — rotate
                if self._active_key_id and self._active_key_id in self._credentials:
                    self._credentials[self._active_key_id].revoked_at = time.time()

                self._credentials[key_id] = RotatableCredential(
                    key_id=key_id,
                    api_key=raw_key,
                    provider=self._provider,
                    activated_at=time.time(),
                )
                self._active_key_id = key_id
                self._rotation_count += 1

            cred = self._credentials.get(self._active_key_id)
            if cred:
                cred.in_flight_count += 1
            return raw_key

    def release_key(self, api_key: str) -> None:
        key_id = self._make_key_id(api_key)
        with self._lock:
            cred = self._credentials.get(key_id)
            if cred:
                cred.in_flight_count = max(0, cred.in_flight_count - 1)
                # Clean up drained revoked credentials
                if cred.can_drain() and key_id != self._active_key_id:
                    del self._credentials[key_id]

    def rotation_count(self) -> int:
        return self._rotation_count

    def status(self) -> dict:
        with self._lock:
            return {
                "active_key_id": self._active_key_id,
                "credential_count": len(self._credentials),
                "rotation_count": self._rotation_count,
                "credentials": [
                    {
                        "key_id": cred.key_id,
                        "active": cred.is_active(),
                        "in_flight": cred.in_flight_count,
                        "fingerprint": cred.fingerprint(),
                    }
                    for cred in self._credentials.values()
                ],
            }
```

## Solution 4: Rotation-Aware LLM Client

```python
import asyncio
from typing import Any, Callable, Dict, List, Optional


class RotationAwareLLMClient:
    """
    Wraps LLM API calls with automatic key acquisition and release
    so the rotator can track in-flight usage accurately.
    """

    def __init__(
        self,
        rotator: ZeroDowntimeKeyRotator,
        call_fn: Callable,
    ):
        self._rotator = rotator
        self._call_fn = call_fn

    async def complete(
        self,
        messages: List[Dict[str, Any]],
        *,
        model: str,
        max_tokens: int = 1024,
        system: Optional[str] = None,
    ) -> Any:
        api_key = self._rotator.current_key()
        try:
            return await self._call_fn(
                messages=messages,
                model=model,
                max_tokens=max_tokens,
                system=system,
                api_key=api_key,
            )
        finally:
            self._rotator.release_key(api_key)
```

## Solution 5: Key Rotation Health Monitor

```python
import asyncio
import time
from typing import Optional


class KeyRotationHealthMonitor:
    """
    Periodically checks whether the credential source has a new key,
    logs rotation events, and alerts if rotation fails or stalls.
    """

    def __init__(
        self,
        rotator: ZeroDowntimeKeyRotator,
        loader: LiveCredentialLoader,
        check_interval_seconds: float = 30.0,
        alert_fn=None,
    ):
        self._rotator = rotator
        self._loader = loader
        self._interval = check_interval_seconds
        self._alert_fn = alert_fn
        self._last_rotation_at: Optional[float] = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._running = False

    async def _run(self) -> None:
        prev_count = self._rotator.rotation_count()
        while self._running:
            await asyncio.sleep(self._interval)
            # Force key check
            self._rotator.current_key()
            new_count = self._rotator.rotation_count()
            if new_count > prev_count:
                self._last_rotation_at = time.time()
                if self._alert_fn:
                    self._alert_fn({
                        "event": "key_rotation",
                        "rotation_count": new_count,
                        "rotated_at": self._last_rotation_at,
                        "status": self._rotator.status(),
                    })
                prev_count = new_count

    def health(self) -> dict:
        return {
            "rotation_count": self._rotator.rotation_count(),
            "last_rotation_at": self._last_rotation_at,
            "current_status": self._rotator.status(),
        }
```

## Solution 6: Key Rotation Audit Logger

```python
import time
from typing import List


class KeyRotationAuditLogger:
    """
    Records every key rotation event with timing and in-flight
    drain status for compliance and security audit trails.
    """

    def __init__(self):
        self._events: List[dict] = []

    def record_rotation(self, old_key_id: str, new_key_id: str, in_flight_at_rotation: int) -> None:
        self._events.append({
            "event": "rotation",
            "old_key_id": old_key_id,
            "new_key_id": new_key_id,
            "in_flight_at_rotation": in_flight_at_rotation,
            "rotated_at": time.time(),
        })

    def record_drain(self, key_id: str) -> None:
        self._events.append({
            "event": "drained",
            "key_id": key_id,
            "drained_at": time.time(),
        })

    def summary(self) -> dict:
        rotations = [e for e in self._events if e["event"] == "rotation"]
        return {
            "total_rotations": len(rotations),
            "last_rotation_at": rotations[-1]["rotated_at"] if rotations else None,
            "avg_in_flight_at_rotation": round(
                sum(r["in_flight_at_rotation"] for r in rotations) / max(len(rotations), 1), 2
            ),
        }
```

## Comparison

| Approach | Live Key Loading | Zero-Downtime Rotation | In-Flight Tracking | Health Monitoring | Audit |
|---|---|---|---|---|---|
| LiveCredentialLoader | Yes (env/file/fn) | No | No | No | No |
| ZeroDowntimeKeyRotator | Via loader | Yes (drain on revoke) | Yes (counter) | No | No |
| RotationAwareLLMClient | No | Via rotator | Via rotator | No | No |
| KeyRotationHealthMonitor | No | No | No | Yes (async) | No |
| KeyRotationAuditLogger | No | No | No | No | Yes |

**Best for production**: Set `check_interval_seconds=30` on `LiveCredentialLoader` so new keys are picked up within 30 seconds of being placed in the secrets manager — fast enough for incident response without causing excessive secret reads. Use `ZeroDowntimeKeyRotator.status()` to verify that `in_flight_count` for the old key reaches zero within `drain_timeout_seconds` before revoking it at the secrets manager level. Alert when a rotation is detected (`KeyRotationHealthMonitor`) so the security team has a timestamped record of every rotation — this is essential for forensic analysis if a key was leaked and the rotation timeline needs to be established.
