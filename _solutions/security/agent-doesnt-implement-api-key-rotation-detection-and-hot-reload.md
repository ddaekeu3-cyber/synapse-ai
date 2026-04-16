---
title: "Agent Doesn't Implement API Key Rotation Detection and Hot Reload"
description: "Agents that load API keys once at startup and hold them in memory until the next process restart cannot use rotated credentials without downtime. When a key is rotated for security reasons, the agent continues using the stale key until it starts failing with 401 errors, causing service disruption. Implement hot reload of API credentials that detects rotation events and updates in-process credentials without restarting."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-api-key-rotation-detection-and-hot-reload
tags: [api-key-rotation, credential-hot-reload, secret-refresh, zero-downtime-rotation, credential-lifecycle, vault-integration]
symptoms:
  - "Key rotation causes 401 errors until agent is manually restarted"
  - "Rotated credentials require a deployment to take effect"
  - "Agent has no mechanism to detect that a secret in the vault or environment has changed"
  - "30-second outage during every scheduled key rotation"
  - "Cannot perform emergency key rotation without service interruption"
---

## Why This Happens

Most agents load credentials at startup from environment variables or secret stores and bind them into HTTP client objects that are initialized once. When the underlying secret is rotated, the client still holds the old key. The agent learns of the rotation only when the old key is revoked and API calls start returning 401. Hot reload requires a separate background process that watches for credential changes (by polling a vault, watching a file mtime, or listening to a rotation event), fetches the new value, and swaps it into all active clients without requiring a restart.

## Solution 1: Credential Holder

```python
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class CredentialVersion:
    key_id: str
    secret_value: str
    version: int
    loaded_at: float = field(default_factory=time.time)
    source: str = ""    # "env" | "vault" | "k8s_secret" | "aws_ssm"
    expires_at: Optional[float] = None

    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    def age_seconds(self) -> float:
        return round(time.time() - self.loaded_at, 1)


class HotReloadableCredential:
    """
    Thread-safe credential holder that supports atomic swap of the
    active credential version. Callers always receive the current version.
    """

    def __init__(self, name: str, initial: CredentialVersion):
        self._name = name
        self._current = initial
        self._lock = threading.RLock()
        self._version_history: list = [initial.version]
        self._reload_count = 0
        self._on_reload_callbacks: list = []

    def get(self) -> CredentialVersion:
        with self._lock:
            return self._current

    def reload(self, new_version: CredentialVersion) -> bool:
        """Swap to new credentials. Returns True if the value changed."""
        with self._lock:
            if new_version.secret_value == self._current.secret_value:
                return False
            self._current = new_version
            self._version_history.append(new_version.version)
            self._reload_count += 1

        for cb in self._on_reload_callbacks:
            try:
                cb(self._name, new_version)
            except Exception:
                pass
        return True

    def on_reload(self, callback: Callable) -> None:
        self._on_reload_callbacks.append(callback)

    def stats(self) -> dict:
        with self._lock:
            return {
                "name": self._name,
                "current_version": self._current.version,
                "reload_count": self._reload_count,
                "credential_age_seconds": self._current.age_seconds(),
                "source": self._current.source,
            }
```

## Solution 2: Credential Source Poller

```python
import asyncio
import os
import time
from pathlib import Path
from typing import Callable, Dict, Optional


class EnvironmentCredentialPoller:
    """
    Polls environment variables or files for credential changes.
    Suitable for Kubernetes secret mounts (file-based) or dynamic env injection.
    """

    def __init__(
        self,
        credential: HotReloadableCredential,
        env_var: Optional[str] = None,
        file_path: Optional[str] = None,
        poll_interval_seconds: float = 30.0,
    ):
        self._credential = credential
        self._env_var = env_var
        self._file_path = Path(file_path) if file_path else None
        self._interval = poll_interval_seconds
        self._task: Optional[asyncio.Task] = None
        self._last_mtime: Optional[float] = None
        self._poll_count = 0

    async def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self._check()
            self._poll_count += 1

    async def _check(self) -> None:
        new_value = None

        if self._file_path and self._file_path.exists():
            mtime = self._file_path.stat().st_mtime
            if mtime != self._last_mtime:
                self._last_mtime = mtime
                new_value = self._file_path.read_text().strip()

        elif self._env_var:
            env_value = os.getenv(self._env_var)
            current = self._credential.get()
            if env_value and env_value != current.secret_value:
                new_value = env_value

        if new_value:
            current = self._credential.get()
            new_version = CredentialVersion(
                key_id=current.key_id,
                secret_value=new_value,
                version=current.version + 1,
                source=self._credential._current.source,
            )
            self._credential.reload(new_version)
```

## Solution 3: Client Auto-Updater

```python
import threading
from typing import Any, Callable, Dict, Optional


class ClientAutoUpdater:
    """
    Listens for credential reload events and rebuilds registered
    HTTP/API clients with the new credentials automatically.
    """

    def __init__(self, credential: HotReloadableCredential):
        self._credential = credential
        self._clients: Dict[str, tuple] = {}
        # name -> (client_ref_container, factory_fn)
        self._lock = threading.Lock()
        self._rebuild_count = 0
        credential.on_reload(self._on_credential_reload)

    def register_client(
        self,
        client_name: str,
        client_container: list,   # single-element list for mutability
        factory_fn: Callable,
    ) -> None:
        with self._lock:
            self._clients[client_name] = (client_container, factory_fn)

    def _on_credential_reload(self, cred_name: str, new_version: CredentialVersion) -> None:
        with self._lock:
            clients_to_rebuild = list(self._clients.items())

        for client_name, (container, factory_fn) in clients_to_rebuild:
            try:
                new_client = factory_fn(new_version.secret_value)
                container[0] = new_client
                self._rebuild_count += 1
            except Exception:
                pass  # keep using old client on factory failure

    def stats(self) -> dict:
        return {
            "registered_clients": len(self._clients),
            "rebuild_count": self._rebuild_count,
        }
```

## Solution 4: Rotation Event Validator

```python
import time
from typing import List, Optional


class RotationEventValidator:
    """
    Validates that a new credential is functional before completing
    the hot reload. Prevents swapping to a broken new key that would
    cause more disruption than continuing with the old one.
    """

    def __init__(self, validation_fn: Optional[callable] = None):
        self._validate = validation_fn
        self._validations: List[dict] = []

    async def validate_and_reload(
        self,
        credential: HotReloadableCredential,
        candidate: CredentialVersion,
    ) -> dict:
        if self._validate is None:
            reloaded = credential.reload(candidate)
            return {"reloaded": reloaded, "validated": False}

        start = time.time()
        try:
            is_valid = await self._validate(candidate.secret_value)
            latency_ms = round((time.time() - start) * 1000, 2)
        except Exception as exc:
            latency_ms = round((time.time() - start) * 1000, 2)
            self._validations.append({
                "ts": time.time(),
                "success": False,
                "error": str(exc),
                "latency_ms": latency_ms,
            })
            return {"reloaded": False, "validated": False, "error": str(exc)}

        if not is_valid:
            self._validations.append({
                "ts": time.time(),
                "success": False,
                "error": "validation returned False",
                "latency_ms": latency_ms,
            })
            return {"reloaded": False, "validated": True, "valid": False}

        reloaded = credential.reload(candidate)
        self._validations.append({
            "ts": time.time(),
            "success": True,
            "latency_ms": latency_ms,
        })
        return {"reloaded": reloaded, "validated": True, "valid": True}
```

## Solution 5: Rotation Audit Logger

```python
import time
from typing import List


class CredentialRotationAuditLogger:
    """
    Records all credential reload events with version numbers and source
    for security audit and key lifecycle tracking.
    """

    def __init__(self, max_records: int = 1000):
        self._max = max_records
        self._records: List[dict] = []

    def setup(self, credential: HotReloadableCredential) -> None:
        credential.on_reload(self._on_reload)

    def _on_reload(self, cred_name: str, new_version: CredentialVersion) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "credential_name": cred_name,
            "new_version": new_version.version,
            "source": new_version.source,
            "key_id": new_version.key_id,
        })

    def recent(self, count: int = 20) -> List[dict]:
        return self._records[-count:]

    def rotation_count(self, window_seconds: float = 86400.0) -> int:
        cutoff = time.time() - window_seconds
        return sum(1 for r in self._records if r["ts"] >= cutoff)
```

## Solution 6: Hot Reload Dashboard

```python
import time


class CredentialHotReloadDashboard:
    """
    Combines credential stats, client auto-updater stats, and
    rotation audit log into a credential lifecycle health report.
    """

    def __init__(
        self,
        credential: HotReloadableCredential,
        updater: ClientAutoUpdater,
        audit_logger: CredentialRotationAuditLogger,
    ):
        self._credential = credential
        self._updater = updater
        self._audit = audit_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "credential": self._credential.stats(),
            "client_updater": self._updater.stats(),
            "rotations_24h": self._audit.rotation_count(window_seconds=86400.0),
            "recent_rotations": self._audit.recent(5),
        }
```

## Comparison

| Approach | In-Memory Swap | Change Detection | Client Rebuild | Pre-Reload Validation | Audit |
|---|---|---|---|---|---|
| HotReloadableCredential | Yes (atomic) | No | No | No | No |
| EnvironmentCredentialPoller | No | Yes (mtime/env) | No | No | No |
| ClientAutoUpdater | No | Via callback | Yes (factory) | No | No |
| RotationEventValidator | No | No | No | Yes (pre-swap) | No |
| CredentialRotationAuditLogger | No | No | No | No | Yes |
| CredentialHotReloadDashboard | No | No | No | No | Yes |

**Best for production**: Use file-based polling (`file_path`) for Kubernetes secret mounts — Kubernetes updates the mounted file atomically when a secret is rotated, and watching mtime is more reliable than comparing env vars across process boundaries. Set `poll_interval_seconds=30` for routine rotation detection; for emergency rotation, also expose a `/reload-credentials` admin endpoint that triggers an immediate check. Always validate new credentials before swapping (`RotationEventValidator`) — a broken new key is worse than a working old key. Emit a structured log event on every reload that includes `key_id` and `version` so the rotation audit trail is in your centralized log platform, not just in the in-process `CredentialRotationAuditLogger`.
