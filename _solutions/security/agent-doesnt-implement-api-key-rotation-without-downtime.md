---
title: "Agent Doesn't Implement API Key Rotation Without Downtime"
description: "Agents that store a single API key in an environment variable have no mechanism to rotate credentials without a deployment restart: the key must be updated, the process restarted, and requests fail during the gap. Implement a live key rotation system that loads new credentials from a secret store, validates them before promotion, gracefully drains in-flight requests, and atomically swaps the active key — achieving zero-downtime rotation."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-api-key-rotation-without-downtime
tags: [api-key-rotation, zero-downtime, secret-management, credential-refresh, atomic-swap, live-rotation]
symptoms:
  - "Rotating an API key requires a full agent restart, causing request failures during the gap"
  - "Expired keys are discovered only when requests start failing in production"
  - "No mechanism to validate a new key before promoting it as the active credential"
  - "In-flight requests using the old key fail immediately when the environment variable is overwritten"
  - "Key rotation events are not logged, making audit trails for compliance impossible"
---

## Why This Happens

Environment variable–based credentials are read once at process startup and held for the lifetime of the process. Rotating the key in the secret store has no effect until the process restarts. The restart window — however brief — drops in-flight requests and resets connection pools. A live rotation system must solve three problems: discovery (detecting that a new key is available), validation (proving the new key works before promoting it), and drain (allowing in-flight requests using the old key to complete before the old key is invalidated). This requires an abstraction layer between the agent and the raw credential value so the active key can be swapped atomically at runtime.

## Solution 1: API Key Version Record

```python
import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class KeyStatus(str, Enum):
    PENDING = "pending"       # loaded but not yet validated
    ACTIVE = "active"         # current key in use
    DRAINING = "draining"     # being phased out; in-flight requests may still use it
    RETIRED = "retired"       # no longer valid for new requests


@dataclass
class APIKeyVersion:
    version_id: str
    key_value: str            # the raw secret value
    status: KeyStatus
    created_at: float = field(default_factory=time.time)
    activated_at: Optional[float] = None
    retired_at: Optional[float] = None
    source: str = ""          # "env" | "vault" | "aws_secrets_manager" | "manual"

    @property
    def fingerprint(self) -> str:
        """SHA-256 prefix for logging without exposing the key."""
        return hashlib.sha256(self.key_value.encode()).hexdigest()[:12]

    def activate(self) -> None:
        self.status = KeyStatus.ACTIVE
        self.activated_at = time.time()

    def retire(self) -> None:
        self.status = KeyStatus.RETIRED
        self.retired_at = time.time()
```

## Solution 2: Key Validator

```python
import asyncio
from typing import Any, Callable, Optional


class APIKeyValidator:
    """
    Validates a candidate key by making a lightweight probe request to the
    target API. Promotion is blocked until validation succeeds.
    """

    def __init__(
        self,
        probe_fn: Callable[[str], Any],   # async fn(key) -> raises on failure
        timeout_seconds: float = 5.0,
        max_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
    ):
        self._probe = probe_fn
        self._timeout = timeout_seconds
        self._max_attempts = max_attempts
        self._retry_delay = retry_delay_seconds

    async def validate(self, key_version: APIKeyVersion) -> bool:
        for attempt in range(1, self._max_attempts + 1):
            try:
                await asyncio.wait_for(
                    self._probe(key_version.key_value),
                    timeout=self._timeout,
                )
                return True
            except asyncio.TimeoutError:
                if attempt == self._max_attempts:
                    return False
            except Exception:
                if attempt == self._max_attempts:
                    return False
                await asyncio.sleep(self._retry_delay)
        return False
```

## Solution 3: In-Flight Request Tracker

```python
import asyncio
import time
from contextlib import asynccontextmanager
from typing import Dict, Set


class InFlightRequestTracker:
    """
    Tracks which requests are currently using a given key version.
    Rotation waits until all requests using the old key version complete.
    """

    def __init__(self):
        self._active: Dict[str, Set[str]] = {}   # version_id -> set of request_ids
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def track(self, version_id: str, request_id: str):
        async with self._lock:
            if version_id not in self._active:
                self._active[version_id] = set()
            self._active[version_id].add(request_id)
        try:
            yield
        finally:
            async with self._lock:
                self._active.get(version_id, set()).discard(request_id)

    async def wait_for_drain(
        self,
        version_id: str,
        timeout_seconds: float = 30.0,
        poll_interval: float = 0.1,
    ) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            async with self._lock:
                count = len(self._active.get(version_id, set()))
            if count == 0:
                return True
            await asyncio.sleep(poll_interval)
        return False

    def active_count(self, version_id: str) -> int:
        return len(self._active.get(version_id, set()))
```

## Solution 4: Live Key Rotator

```python
import asyncio
import time
import uuid
from typing import Optional


class LiveAPIKeyRotator:
    """
    Atomically rotates the active API key with zero downtime.
    Validates the new key, drains old in-flight requests, then promotes.
    """

    def __init__(
        self,
        validator: APIKeyValidator,
        tracker: InFlightRequestTracker,
        drain_timeout_seconds: float = 30.0,
    ):
        self._validator = validator
        self._tracker = tracker
        self._drain_timeout = drain_timeout_seconds
        self._active: Optional[APIKeyVersion] = None
        self._lock = asyncio.Lock()

    async def initialize(self, key_value: str, source: str = "env") -> APIKeyVersion:
        version = APIKeyVersion(
            version_id=str(uuid.uuid4()),
            key_value=key_value,
            status=KeyStatus.PENDING,
            source=source,
        )
        valid = await self._validator.validate(version)
        if not valid:
            raise ValueError(f"Initial key validation failed (fingerprint={version.fingerprint})")
        version.activate()
        async with self._lock:
            self._active = version
        return version

    async def rotate(self, new_key_value: str, source: str = "manual") -> dict:
        candidate = APIKeyVersion(
            version_id=str(uuid.uuid4()),
            key_value=new_key_value,
            status=KeyStatus.PENDING,
            source=source,
        )

        # Validate before touching the active key
        valid = await self._validator.validate(candidate)
        if not valid:
            return {
                "success": False,
                "reason": "validation_failed",
                "candidate_fingerprint": candidate.fingerprint,
            }

        async with self._lock:
            old_version = self._active
            if old_version:
                old_version.status = KeyStatus.DRAINING

        # Wait for old key's in-flight requests to finish
        if old_version:
            drained = await self._tracker.wait_for_drain(
                old_version.version_id, self._drain_timeout
            )
            if not drained:
                remaining = self._tracker.active_count(old_version.version_id)
                # Promote anyway — remaining requests will get errors on their own
                pass
            old_version.retire()

        candidate.activate()
        async with self._lock:
            self._active = candidate

        return {
            "success": True,
            "new_version_id": candidate.version_id,
            "new_fingerprint": candidate.fingerprint,
            "old_version_id": old_version.version_id if old_version else None,
            "rotated_at": time.time(),
        }

    def current_key(self) -> Optional[str]:
        return self._active.key_value if self._active else None

    def current_version(self) -> Optional[APIKeyVersion]:
        return self._active
```

## Solution 5: Secret Store Poller

```python
import asyncio
import time
from typing import Callable, Optional


class SecretStorePoller:
    """
    Polls a secret store (Vault, AWS Secrets Manager, etc.) for key updates
    and triggers rotation automatically when a new version is detected.
    """

    def __init__(
        self,
        rotator: LiveAPIKeyRotator,
        fetch_fn: Callable[[], str],     # async fn() -> current key value from store
        poll_interval_seconds: float = 60.0,
        source_label: str = "secret_store",
    ):
        self._rotator = rotator
        self._fetch = fetch_fn
        self._interval = poll_interval_seconds
        self._source = source_label
        self._last_fingerprint: Optional[str] = None
        self._rotation_count = 0
        self._last_rotation_at: Optional[float] = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        while self._running:
            try:
                new_key = await self._fetch()
                fingerprint = __import__("hashlib").sha256(new_key.encode()).hexdigest()[:12]
                if fingerprint != self._last_fingerprint:
                    result = await self._rotator.rotate(new_key, source=self._source)
                    if result["success"]:
                        self._last_fingerprint = fingerprint
                        self._rotation_count += 1
                        self._last_rotation_at = time.time()
            except Exception:
                pass  # next poll will retry
            await asyncio.sleep(self._interval)

    def stop(self) -> None:
        self._running = False

    def stats(self) -> dict:
        return {
            "rotation_count": self._rotation_count,
            "last_rotation_at": self._last_rotation_at,
            "poll_interval_seconds": self._interval,
        }
```

## Solution 6: Key Rotation Audit Logger

```python
import time
from typing import List


class KeyRotationAuditLogger:
    """
    Records every key rotation event for compliance audit trails.
    Stores fingerprints, not raw key values.
    """

    def __init__(self, max_records: int = 1000):
        self._max = max_records
        self._records: List[dict] = []

    def record_rotation(self, rotation_result: dict, trigger: str = "manual") -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "trigger": trigger,
            "success": rotation_result.get("success", False),
            "new_fingerprint": rotation_result.get("new_fingerprint"),
            "old_version_id": rotation_result.get("old_version_id"),
            "new_version_id": rotation_result.get("new_version_id"),
            "reason": rotation_result.get("reason"),
        })

    def record_validation_failure(self, fingerprint: str, reason: str) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "event": "validation_failure",
            "fingerprint": fingerprint,
            "reason": reason,
        })

    def recent(self, limit: int = 20) -> List[dict]:
        return self._records[-limit:]

    def summary(self) -> dict:
        total = len(self._records)
        successes = sum(1 for r in self._records if r.get("success"))
        return {
            "total_events": total,
            "successful_rotations": successes,
            "failed_rotations": total - successes,
        }
```

## Comparison

| Approach | Zero-Downtime | Pre-Promotion Validation | Request Drain | Auto-Poll Secret Store | Audit Log |
|---|---|---|---|---|---|
| APIKeyVersion | No | No | No | No | No |
| APIKeyValidator | No | Yes (probe + retry) | No | No | No |
| InFlightRequestTracker | No | No | Yes (wait + timeout) | No | No |
| LiveAPIKeyRotator | Yes (atomic swap) | Via validator | Via tracker | No | No |
| SecretStorePoller | Via rotator | Via rotator | Via rotator | Yes (fingerprint diff) | No |
| KeyRotationAuditLogger | No | No | No | No | Yes |

**Best for production**: Wire `SecretStorePoller` to your secret manager's current-version endpoint and set `poll_interval_seconds=60` — this bounds the window between key issuance and promotion to one minute without burdening the secret store. Always validate before promoting: a misconfigured new key that fails validation leaves the old key active, preventing an outage. Set `drain_timeout_seconds=30` — most agent requests complete within five seconds; thirty seconds accommodates the long tail. Log every rotation event via `KeyRotationAuditLogger` and emit the record to your SIEM: compliance frameworks require a complete audit trail of which credential was active during each time window.
