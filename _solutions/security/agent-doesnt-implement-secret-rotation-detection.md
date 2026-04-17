---
title: "Agent Doesn't Implement Secret Rotation Detection"
description: "Agents that load API keys and secrets once at startup continue using stale credentials after rotation — causing 401 errors that persist until the next deployment, and leaving the old (now-revoked) credential cached in memory while the new one is available in the secret store. Implement secret rotation detection that polls the secret store for version changes, hot-reloads rotated credentials without requiring a restart, and alerts on authentication failures that indicate a rotation may have occurred."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-secret-rotation-detection
tags: [secret-rotation, credential-management, hot-reload, secrets-manager, api-key-rotation, zero-downtime-rotation]
symptoms:
  - "Agent starts returning 401 errors after a planned secret rotation — requires restart to fix"
  - "API keys loaded at startup are still in use hours after rotation — old key traffic logs show post-rotation activity"
  - "No mechanism to detect that a secret version has changed in AWS Secrets Manager or Vault"
  - "Secret rotation causes a gap in service availability while the new deployment rolls out"
  - "Authentication failures are not used as a signal to check for rotated credentials"
---

## Why This Happens

Secrets loaded at startup are captured in memory and used for the lifetime of the process. When a secret is rotated in AWS Secrets Manager, HashiCorp Vault, or GCP Secret Manager, the process does not know — it continues using the old value. If the old value is revoked immediately (or after a short overlap window), every subsequent authenticated call fails with 401. Without a rotation detection mechanism, the only recovery is to restart the process. Hot-reload requires three components: a version-aware secret reader that tracks the last-seen version, a poller that checks for version changes periodically, and a credential update path that replaces the in-use credential without restarting.

## Solution 1: Secret Version Record

```python
import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SecretVersionRecord:
    secret_name: str
    version_id: str
    value: str
    loaded_at: float = field(default_factory=time.time)
    source: str = ""    # "aws_secrets_manager" | "vault" | "env" | "file"

    def age_seconds(self) -> float:
        return time.time() - self.loaded_at
```

## Solution 2: Secret Store Reader

```python
import os
from typing import Optional


class SecretStoreReader:
    """
    Reads secrets from a configured backend.
    Supports AWS Secrets Manager, environment variables, and file-based secrets.
    """

    def __init__(self, source: str = "env"):
        self._source = source

    def read(self, secret_name: str) -> Optional[SecretVersionRecord]:
        if self._source == "env":
            return self._read_env(secret_name)
        if self._source == "aws_secrets_manager":
            return self._read_aws(secret_name)
        if self._source == "file":
            return self._read_file(secret_name)
        return None

    def _read_env(self, secret_name: str) -> Optional[SecretVersionRecord]:
        value = os.getenv(secret_name)
        if value is None:
            return None
        import hashlib
        version = hashlib.sha256(value.encode()).hexdigest()[:8]
        return SecretVersionRecord(
            secret_name=secret_name,
            version_id=version,
            value=value,
            source="env",
        )

    def _read_aws(self, secret_name: str) -> Optional[SecretVersionRecord]:
        try:
            import boto3
            client = boto3.client("secretsmanager")
            response = client.get_secret_value(SecretId=secret_name)
            return SecretVersionRecord(
                secret_name=secret_name,
                version_id=response.get("VersionId", "unknown"),
                value=response.get("SecretString", ""),
                source="aws_secrets_manager",
            )
        except Exception:
            return None

    def _read_file(self, secret_name: str) -> Optional[SecretVersionRecord]:
        import hashlib
        from pathlib import Path
        path = Path(secret_name)
        if not path.exists():
            return None
        value = path.read_text().strip()
        version = hashlib.sha256(value.encode()).hexdigest()[:8]
        return SecretVersionRecord(
            secret_name=secret_name,
            version_id=version,
            value=value,
            source="file",
        )
```

## Solution 3: Secret Rotation Detector

```python
import time
from threading import Lock
from typing import Callable, Dict, Optional


class SecretRotationDetector:
    """
    Tracks the last-seen version of each secret.
    Detects rotation by comparing the current version against the cached version.
    Fires a callback when rotation is detected.
    """

    def __init__(
        self,
        reader: SecretStoreReader,
        on_rotation: Optional[Callable[[str, SecretVersionRecord], None]] = None,
    ):
        self._reader = reader
        self._on_rotation = on_rotation or self._default_callback
        self._known_versions: Dict[str, str] = {}
        self._records: Dict[str, SecretVersionRecord] = {}
        self._lock = Lock()

    @staticmethod
    def _default_callback(secret_name: str, new_record: SecretVersionRecord) -> None:
        import json
        print(json.dumps({
            "event": "secret_rotated",
            "secret_name": secret_name,
            "new_version_id": new_record.version_id,
        }))

    def check(self, secret_name: str) -> tuple:
        """
        Returns (rotated: bool, current_record: SecretVersionRecord | None).
        """
        current = self._reader.read(secret_name)
        if current is None:
            return False, None

        with self._lock:
            known_version = self._known_versions.get(secret_name)
            if known_version is None:
                # First time seeing this secret
                self._known_versions[secret_name] = current.version_id
                self._records[secret_name] = current
                return False, current

            if known_version != current.version_id:
                self._known_versions[secret_name] = current.version_id
                self._records[secret_name] = current
                self._on_rotation(secret_name, current)
                return True, current

        return False, current

    def current_record(self, secret_name: str) -> Optional[SecretVersionRecord]:
        with self._lock:
            return self._records.get(secret_name)
```

## Solution 4: Hot-Reload Secret Manager

```python
import asyncio
import time
from typing import Callable, Dict, Optional, Set


class HotReloadSecretManager:
    """
    Manages a set of watched secrets, polling for rotation at a configurable interval.
    Delivers updated values to registered consumers via callbacks.
    """

    def __init__(
        self,
        detector: SecretRotationDetector,
        poll_interval_seconds: float = 60.0,
    ):
        self._detector = detector
        self._interval = poll_interval_seconds
        self._watched: Set[str] = set()
        self._consumers: Dict[str, list] = {}
        self._running = False
        self._poll_count = 0
        self._rotation_count = 0

    def watch(
        self,
        secret_name: str,
        consumer_fn: Optional[Callable[[SecretVersionRecord], None]] = None,
    ) -> None:
        self._watched.add(secret_name)
        if consumer_fn:
            if secret_name not in self._consumers:
                self._consumers[secret_name] = []
            self._consumers[secret_name].append(consumer_fn)
        # Load immediately
        rotated, record = self._detector.check(secret_name)
        if record and consumer_fn and not rotated:
            consumer_fn(record)   # deliver initial value

    async def start_polling(self) -> None:
        self._running = True
        while self._running:
            await asyncio.sleep(self._interval)
            self._poll_count += 1
            for secret_name in list(self._watched):
                rotated, record = self._detector.check(secret_name)
                if rotated and record:
                    self._rotation_count += 1
                    for consumer in self._consumers.get(secret_name, []):
                        try:
                            consumer(record)
                        except Exception:
                            pass

    def stop(self) -> None:
        self._running = False

    def stats(self) -> dict:
        return {
            "watched_secrets": len(self._watched),
            "poll_count": self._poll_count,
            "rotation_count": self._rotation_count,
        }
```

## Solution 5: Auth Failure Rotation Trigger

```python
import time
from threading import Lock
from typing import Callable, Optional


class AuthFailureRotationTrigger:
    """
    Monitors authentication failures and triggers an immediate rotation check
    when the failure rate exceeds a threshold — catching rotations that happen
    between polling intervals.
    """

    def __init__(
        self,
        manager: HotReloadSecretManager,
        secret_name: str,
        failure_threshold: int = 3,
        window_seconds: float = 60.0,
    ):
        self._manager = manager
        self._secret = secret_name
        self._threshold = failure_threshold
        self._window = window_seconds
        self._failure_times: list = []
        self._lock = Lock()
        self._triggered_count = 0

    def record_auth_failure(self, error_code: int) -> bool:
        """
        Record a 401 error. Returns True if a rotation check was triggered.
        """
        if error_code != 401:
            return False

        now = time.time()
        with self._lock:
            self._failure_times.append(now)
            cutoff = now - self._window
            self._failure_times = [t for t in self._failure_times if t >= cutoff]
            count = len(self._failure_times)

        if count >= self._threshold:
            self._triggered_count += 1
            rotated, _ = self._manager._detector.check(self._secret)
            if rotated:
                # Immediately notify consumers
                record = self._manager._detector.current_record(self._secret)
                if record:
                    for consumer in self._manager._consumers.get(self._secret, []):
                        try:
                            consumer(record)
                        except Exception:
                            pass
            return True
        return False

    def stats(self) -> dict:
        return {
            "secret_name": self._secret,
            "triggered_rotation_checks": self._triggered_count,
        }
```

## Solution 6: Secret Rotation Dashboard

```python
import time


class SecretRotationDashboard:
    """
    Combines manager stats, known secret versions, and rotation
    trigger history into a single operational view.
    """

    def __init__(
        self,
        manager: HotReloadSecretManager,
        detector: SecretRotationDetector,
        trigger: Optional[AuthFailureRotationTrigger] = None,
    ):
        self._manager = manager
        self._detector = detector
        self._trigger = trigger

    def render(self) -> dict:
        watched = list(self._manager._watched)
        versions = {}
        for name in watched:
            record = self._detector.current_record(name)
            if record:
                versions[name] = {
                    "version_id": record.version_id,
                    "age_seconds": round(record.age_seconds(), 1),
                    "source": record.source,
                }

        report = {
            "generated_at": time.time(),
            "manager_stats": self._manager.stats(),
            "current_versions": versions,
        }
        if self._trigger:
            report["auth_failure_trigger"] = self._trigger.stats()
        return report
```

## Comparison

| Approach | Version Tracking | Change Detection | Hot Reload | Auth Failure Trigger | Dashboard |
|---|---|---|---|---|---|
| SecretRotationDetector | Yes (version ID) | Yes (callback) | No | No | No |
| HotReloadSecretManager | Via detector | Via detector | Yes (polling) | No | No |
| AuthFailureRotationTrigger | No | Via manager | No | Yes (401 threshold) | No |
| SecretRotationDashboard | No | No | No | No | Yes |

**Best for production**: Set `poll_interval_seconds=60` for most secrets — AWS Secrets Manager and Vault have rotation windows measured in minutes, so 60-second polling catches rotations well within the overlap window where both old and new credentials are valid. Pair polling with `AuthFailureRotationTrigger` as a fallback: if rotation happens between polls and the old credential is immediately revoked, the first 401 triggers an immediate re-check. Register a consumer function for each API client that replaces the credential in the client's session — for the Anthropic SDK, this means constructing a new `Anthropic(api_key=new_record.value)` client and replacing the reference. Never log the secret value itself in the rotation event — log only the `version_id` and `secret_name`.
