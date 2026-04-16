---
title: "Agent Doesn't Implement API Key Rotation Detection"
description: "Agents that hardcode or cache API keys indefinitely fail silently when keys are rotated — requests begin returning 401s, retries exhaust, and the agent crashes with no actionable error. Implement API key rotation detection that distinguishes authentication failures from transient errors, triggers a key refresh from the secret store, retries with the new key, and alerts when rotation is needed but no new key is available."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-api-key-rotation-detection
tags: [api-key-rotation, secret-management, credential-refresh, auth-failure, key-lifecycle, secret-store]
symptoms:
  - "Agent starts returning 401 errors after a key rotation with no self-healing"
  - "Retries on 401 responses amplify the problem rather than refreshing credentials"
  - "No mechanism to distinguish 'key expired' from 'wrong key' or 'service down'"
  - "Secrets are read once at startup and never refreshed during long-running sessions"
  - "On-call gets paged for 401 storms that are actually resolved by re-fetching the secret"
---

## Why This Happens

API keys are typically injected at startup — environment variable, config file, or secret manager read — and stored in memory for the process lifetime. When the key is rotated (scheduled or emergency), the in-memory value becomes stale. Standard retry logic re-sends the same invalid key repeatedly, flooding audit logs with failed auth attempts and triggering rate limits before giving up. Rotation detection requires treating 401 responses as a credential cache invalidation signal, not a permanent failure.

## Solution 1: Credential Rotation Signal

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
import time


class AuthFailureReason(str, Enum):
    EXPIRED = "expired"           # key rotated — try refreshing
    INVALID = "invalid"           # key never valid — alert immediately
    REVOKED = "revoked"           # key explicitly revoked
    QUOTA_EXCEEDED = "quota"      # not an auth failure — handle separately
    UNKNOWN = "unknown"


@dataclass
class CredentialRotationSignal:
    service: str
    key_id: Optional[str]          # partial key ID for logging (never log full key)
    failure_reason: AuthFailureReason
    http_status: int
    response_body_hint: str        # first 100 chars of error body
    detected_at: float = field(default_factory=time.time)

    def is_refreshable(self) -> bool:
        return self.failure_reason in (
            AuthFailureReason.EXPIRED,
            AuthFailureReason.UNKNOWN,
        )
```

## Solution 2: Auth Failure Classifier

```python
import re
from typing import Optional


EXPIRED_PATTERNS = [
    re.compile(r"key.*expired", re.IGNORECASE),
    re.compile(r"token.*expired", re.IGNORECASE),
    re.compile(r"credential.*expired", re.IGNORECASE),
    re.compile(r"api.key.*invalid", re.IGNORECASE),
]

REVOKED_PATTERNS = [
    re.compile(r"revoked", re.IGNORECASE),
    re.compile(r"disabled", re.IGNORECASE),
    re.compile(r"suspended", re.IGNORECASE),
]


class AuthFailureClassifier:
    """
    Classifies an HTTP 401/403 response body into an AuthFailureReason
    to determine whether credential refresh should be attempted.
    """

    def classify(
        self,
        http_status: int,
        response_body: str,
        service: str,
        key_id: Optional[str] = None,
    ) -> CredentialRotationSignal:
        if http_status not in (401, 403):
            return CredentialRotationSignal(
                service=service,
                key_id=key_id,
                failure_reason=AuthFailureReason.UNKNOWN,
                http_status=http_status,
                response_body_hint=response_body[:100],
            )

        hint = response_body[:100]

        for pattern in REVOKED_PATTERNS:
            if pattern.search(response_body):
                return CredentialRotationSignal(
                    service=service,
                    key_id=key_id,
                    failure_reason=AuthFailureReason.REVOKED,
                    http_status=http_status,
                    response_body_hint=hint,
                )

        for pattern in EXPIRED_PATTERNS:
            if pattern.search(response_body):
                return CredentialRotationSignal(
                    service=service,
                    key_id=key_id,
                    failure_reason=AuthFailureReason.EXPIRED,
                    http_status=http_status,
                    response_body_hint=hint,
                )

        # 401 with no recognized pattern — treat as potentially refreshable
        return CredentialRotationSignal(
            service=service,
            key_id=key_id,
            failure_reason=AuthFailureReason.UNKNOWN,
            http_status=http_status,
            response_body_hint=hint,
        )
```

## Solution 3: Credential Cache with TTL

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class RotatingCredentialCache:
    """
    Caches credentials per service with an optional TTL.
    Supports forced invalidation when rotation is detected.
    Thread/coroutine safe via asyncio.Lock.
    """

    def __init__(self, default_ttl_seconds: float = 3600.0) -> None:
        self._default_ttl = default_ttl_seconds
        self._store: Dict[str, tuple] = {}   # service -> (key, fetched_at, ttl)
        self._locks: Dict[str, asyncio.Lock] = {}

    def _lock_for(self, service: str) -> asyncio.Lock:
        if service not in self._locks:
            self._locks[service] = asyncio.Lock()
        return self._locks[service]

    async def get(
        self,
        service: str,
        fetcher: Callable[[], Any],
        ttl_seconds: Optional[float] = None,
    ) -> Any:
        """Return cached credential or fetch a fresh one."""
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        async with self._lock_for(service):
            entry = self._store.get(service)
            if entry:
                key, fetched_at, stored_ttl = entry
                if time.time() - fetched_at < stored_ttl:
                    return key
            # Cache miss or expired — fetch fresh
            key = await fetcher()
            self._store[service] = (key, time.time(), ttl)
            return key

    async def invalidate(self, service: str) -> None:
        """Force re-fetch on next get() — call on rotation detection."""
        async with self._lock_for(service):
            self._store.pop(service, None)

    async def refresh(
        self,
        service: str,
        fetcher: Callable[[], Any],
        ttl_seconds: Optional[float] = None,
    ) -> Any:
        """Invalidate and immediately fetch a fresh credential."""
        await self.invalidate(service)
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        async with self._lock_for(service):
            key = await fetcher()
            self._store[service] = (key, time.time(), ttl)
            return key

    def cached_services(self) -> Dict[str, dict]:
        now = time.time()
        result = {}
        for service, (_, fetched_at, ttl) in self._store.items():
            age = now - fetched_at
            result[service] = {
                "age_seconds": round(age, 1),
                "ttl_seconds": ttl,
                "expires_in_seconds": round(max(0.0, ttl - age), 1),
            }
        return result
```

## Solution 4: Rotation-Aware Request Executor

```python
import asyncio
from typing import Any, Callable, Optional


class RotationAwareRequestExecutor:
    """
    Wraps an HTTP call with rotation-detection retry logic.
    On 401/403, classifies the failure, invalidates the credential cache,
    fetches a fresh key, and retries once before propagating the error.
    """

    def __init__(
        self,
        credential_cache: RotatingCredentialCache,
        classifier: AuthFailureClassifier,
        max_rotation_retries: int = 1,
    ) -> None:
        self._cache = credential_cache
        self._classifier = classifier
        self._max_retries = max_rotation_retries
        self._rotation_events: list = []

    async def execute(
        self,
        service: str,
        request_fn: Callable[[str], Any],  # accepts the api key
        key_fetcher: Callable[[], Any],
        key_id_fn: Optional[Callable[[str], str]] = None,
    ) -> Any:
        """
        request_fn: async callable that takes the api_key and returns a response
        key_fetcher: async callable that returns a fresh api key from the secret store
        key_id_fn: optional fn to extract a loggable key ID from the full key
        """
        key = await self._cache.get(service, key_fetcher)

        for attempt in range(self._max_retries + 1):
            try:
                response = await request_fn(key)
                return response
            except Exception as exc:
                # Extract status code from exception if available
                status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
                body = str(exc)[:200]

                if status not in (401, 403):
                    raise

                key_id = key_id_fn(key) if key_id_fn else None
                signal = self._classifier.classify(status, body, service, key_id)
                self._rotation_events.append(signal)

                if not signal.is_refreshable() or attempt >= self._max_retries:
                    raise

                # Refresh credential and retry
                key = await self._cache.refresh(service, key_fetcher)

        raise RuntimeError("RotationAwareRequestExecutor exhausted retries")

    def rotation_events(self) -> list:
        return list(self._rotation_events)
```

## Solution 5: Key Rotation Alerter

```python
import time
from collections import defaultdict
from typing import Callable, List, Optional


class KeyRotationAlerter:
    """
    Monitors rotation events and fires alerts when rotation is frequent
    (indicating misconfigured TTL) or when revocation is detected
    (indicating a possible security incident).
    """

    def __init__(
        self,
        executor: RotationAwareRequestExecutor,
        revocation_handler: Optional[Callable[[CredentialRotationSignal], None]] = None,
        rotation_storm_threshold: int = 5,
        storm_window_seconds: float = 300.0,
    ) -> None:
        self._executor = executor
        self._revocation_handler = revocation_handler
        self._storm_threshold = rotation_storm_threshold
        self._storm_window = storm_window_seconds

    def check(self) -> List[dict]:
        events = self._executor.rotation_events()
        alerts = []
        now = time.time()

        # Check for revocations
        for event in events:
            if event.failure_reason == AuthFailureReason.REVOKED:
                alerts.append({
                    "type": "key_revoked",
                    "service": event.service,
                    "key_id": event.key_id,
                    "detected_at": event.detected_at,
                    "severity": "critical",
                    "message": f"API key for '{event.service}' appears revoked — security incident possible",
                })
                if self._revocation_handler:
                    try:
                        self._revocation_handler(event)
                    except Exception:
                        pass

        # Check for rotation storms
        by_service: dict = defaultdict(list)
        for event in events:
            if now - event.detected_at <= self._storm_window:
                by_service[event.service].append(event)

        for service, recent in by_service.items():
            if len(recent) >= self._storm_threshold:
                alerts.append({
                    "type": "rotation_storm",
                    "service": service,
                    "count": len(recent),
                    "window_seconds": self._storm_window,
                    "severity": "warning",
                    "recommendation": (
                        f"Reduce credential TTL for '{service}' "
                        "or check secret store replication lag."
                    ),
                })

        return alerts
```

## Solution 6: Key Lifecycle Dashboard

```python
import time


class KeyLifecycleDashboard:
    """
    Combines credential cache status, rotation event history,
    and alerts into a single security operational view.
    """

    def __init__(
        self,
        cache: RotatingCredentialCache,
        alerter: KeyRotationAlerter,
        executor: RotationAwareRequestExecutor,
    ) -> None:
        self._cache = cache
        self._alerter = alerter
        self._executor = executor

    def render(self) -> dict:
        events = self._executor.rotation_events()
        by_reason: dict = defaultdict(int)
        for e in events:
            by_reason[e.failure_reason.value] += 1

        return {
            "generated_at": time.time(),
            "credential_cache": self._cache.cached_services(),
            "rotation_event_summary": {
                "total": len(events),
                "by_reason": dict(by_reason),
            },
            "active_alerts": self._alerter.check(),
        }
```

## Comparison

| Approach | Failure Classification | Credential Refresh | Retry Logic | Revocation Detection | Dashboard |
|---|---|---|---|---|---|
| AuthFailureClassifier | Yes | No | No | Yes | No |
| RotatingCredentialCache | No | Yes (TTL + forced) | No | No | No |
| RotationAwareRequestExecutor | Via classifier | Via cache | Yes (1 retry) | Via classifier | No |
| KeyRotationAlerter | No | No | No | Yes (alert) | No |
| KeyLifecycleDashboard | No | No | No | No | Yes |

**Best for production**: Set credential TTL to 80% of the key's actual rotation interval so normal TTL expiry pre-empts the rotation window. On 401 detection, invalidate and retry exactly once — a second 401 after refresh indicates a deeper problem (revocation, wrong environment) that retries cannot fix. Route `REVOKED` signals to a security on-call channel immediately; they indicate either a compromise or an ops error. Monitor rotation storm rate: more than 5 rotations per service in 5 minutes means the secret store is replicating slowly and you should increase the post-rotation propagation delay.
