---
title: "Agent Doesn't Implement Credential Expiry Detection Before Tool Execution"
description: "Agents that invoke tools without checking whether the underlying credentials are still valid produce cascading failures: an expired OAuth token causes a tool to return a 401, the agent retries with the same credential, accumulates failure counts, and may lock the account or trigger a circuit breaker — all because expiry was not caught before the first call. Implement credential expiry detection that validates token lifetime before each tool execution and triggers refresh or graceful error handling before the first failed request."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-credential-expiry-detection-before-tool-execution
tags: [credential-expiry, token-refresh, oauth, pre-execution-validation, secret-rotation, auth-lifecycle]
symptoms:
  - "Tool calls fail with 401 Unauthorized after credentials expire mid-session"
  - "Agent retries the same expired token repeatedly, triggering account lockout"
  - "No distinction between credential expiry and tool-level errors in error logs"
  - "Session tokens expire silently — agent continues as if authenticated until first failure"
  - "Credential refresh logic lives inside the tool, not in a shared pre-execution check"
---

## Why This Happens

Credentials have lifetimes. OAuth access tokens typically expire in an hour; session cookies in 24 hours; API keys may be rotated on a schedule. Agents that acquire credentials at startup and reuse them without checking expiry assume the token is valid until a 401 proves otherwise. This is a poor strategy: the 401 arrives after the tool call is dispatched, at which point the agent must decide whether to retry, refresh, or abort — under the pressure of an in-flight request. Pre-execution expiry detection moves this check to before the call, when there is time to refresh without disrupting the request flow.

## Solution 1: Credential Record

```python
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional


class CredentialType(str, Enum):
    OAUTH_ACCESS_TOKEN = "oauth_access_token"
    OAUTH_REFRESH_TOKEN = "oauth_refresh_token"
    API_KEY = "api_key"
    SESSION_COOKIE = "session_cookie"
    SERVICE_ACCOUNT_KEY = "service_account_key"


@dataclass
class CredentialRecord:
    credential_id: str
    credential_type: CredentialType
    value: str
    issued_at: float
    expires_at: Optional[float]        # None = no expiry
    refresh_token: Optional[str] = None
    scopes: list = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self, clock_skew_seconds: float = 30.0) -> bool:
        if self.expires_at is None:
            return False
        return time.time() >= self.expires_at - clock_skew_seconds

    def seconds_until_expiry(self) -> Optional[float]:
        if self.expires_at is None:
            return None
        return max(0.0, self.expires_at - time.time())

    def lifetime_fraction_remaining(self) -> Optional[float]:
        if self.expires_at is None:
            return None
        total = self.expires_at - self.issued_at
        if total <= 0:
            return 0.0
        remaining = self.expires_at - time.time()
        return max(0.0, min(1.0, remaining / total))
```

## Solution 2: Credential Store

```python
import threading
import time
from typing import Dict, Optional


class CredentialStore:
    """
    Thread-safe in-memory store for credential records.
    Supports lookup by credential_id and by service name.
    """

    def __init__(self):
        self._credentials: Dict[str, CredentialRecord] = {}
        self._service_map: Dict[str, str] = {}   # service_name -> credential_id
        self._lock = threading.Lock()

    def put(self, record: CredentialRecord, service_name: str = "") -> None:
        with self._lock:
            self._credentials[record.credential_id] = record
            if service_name:
                self._service_map[service_name] = record.credential_id

    def get(self, credential_id: str) -> Optional[CredentialRecord]:
        with self._lock:
            return self._credentials.get(credential_id)

    def get_for_service(self, service_name: str) -> Optional[CredentialRecord]:
        with self._lock:
            cid = self._service_map.get(service_name)
            if cid is None:
                return None
            return self._credentials.get(cid)

    def remove(self, credential_id: str) -> None:
        with self._lock:
            self._credentials.pop(credential_id, None)
            self._service_map = {
                svc: cid for svc, cid in self._service_map.items()
                if cid != credential_id
            }

    def all_expiring_within(self, seconds: float) -> list:
        cutoff = time.time() + seconds
        with self._lock:
            return [
                r for r in self._credentials.values()
                if r.expires_at is not None and r.expires_at <= cutoff
            ]
```

## Solution 3: Credential Expiry Checker

```python
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class ExpiryCheckResult:
    credential_id: str
    is_valid: bool
    is_expired: bool
    seconds_until_expiry: Optional[float]
    needs_refresh: bool           # within proactive refresh window
    recommendation: str


class CredentialExpiryChecker:
    """
    Evaluates a credential record before a tool execution and recommends
    whether to proceed, refresh first, or abort.
    """

    def __init__(
        self,
        clock_skew_seconds: float = 30.0,
        proactive_refresh_threshold_seconds: float = 120.0,
    ):
        self._skew = clock_skew_seconds
        self._proactive = proactive_refresh_threshold_seconds

    def check(self, record: CredentialRecord) -> ExpiryCheckResult:
        if record.is_expired(self._skew):
            return ExpiryCheckResult(
                credential_id=record.credential_id,
                is_valid=False,
                is_expired=True,
                seconds_until_expiry=0.0,
                needs_refresh=True,
                recommendation="expired — refresh required before tool execution",
            )

        remaining = record.seconds_until_expiry()
        needs_refresh = (
            remaining is not None
            and remaining <= self._proactive
            and record.refresh_token is not None
        )

        return ExpiryCheckResult(
            credential_id=record.credential_id,
            is_valid=True,
            is_expired=False,
            seconds_until_expiry=remaining,
            needs_refresh=needs_refresh,
            recommendation=(
                "proactive refresh recommended" if needs_refresh
                else "credential valid"
            ),
        )
```

## Solution 4: Credential Refresher

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class CredentialRefresher:
    """
    Calls a registered refresh handler to obtain a new credential record
    when the existing one is expired or approaching expiry.
    Prevents concurrent refresh storms via per-credential locks.
    """

    def __init__(self):
        self._handlers: Dict[str, Callable] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def register_handler(
        self,
        credential_type: str,
        handler: Callable,
    ) -> None:
        """handler(record: CredentialRecord) -> CredentialRecord"""
        self._handlers[credential_type] = handler

    async def refresh(self, record: CredentialRecord) -> Optional[CredentialRecord]:
        ctype = record.credential_type.value
        handler = self._handlers.get(ctype)
        if handler is None:
            return None

        if record.credential_id not in self._locks:
            self._locks[record.credential_id] = asyncio.Lock()

        async with self._locks[record.credential_id]:
            # Re-check after acquiring lock — another coroutine may have refreshed
            if not record.is_expired(30.0):
                return record
            try:
                new_record = await handler(record)
                return new_record
            except Exception as exc:
                raise CredentialRefreshError(
                    record.credential_id, str(exc)
                ) from exc


class CredentialRefreshError(Exception):
    def __init__(self, credential_id: str, reason: str):
        super().__init__(
            f"failed to refresh credential '{credential_id}': {reason}"
        )
        self.credential_id = credential_id
        self.reason = reason
```

## Solution 5: Pre-Execution Credential Guard

```python
import time
from typing import Any, Callable, Optional


class PreExecutionCredentialGuard:
    """
    Wraps tool execution with credential validation and optional proactive
    refresh. Blocks the tool call if the credential is expired and no refresh
    is available. Emits structured audit events for every check.
    """

    def __init__(
        self,
        store: CredentialStore,
        checker: CredentialExpiryChecker,
        refresher: CredentialRefresher,
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._store = store
        self._checker = checker
        self._refresher = refresher
        self._audit = audit_fn or (lambda ev: None)
        self._checks_total = 0
        self._refreshes_triggered = 0
        self._blocks_total = 0

    async def guard(
        self,
        service_name: str,
        tool_fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        record = self._store.get_for_service(service_name)
        self._checks_total += 1

        if record is None:
            self._audit({
                "event": "credential_guard_skip",
                "service": service_name,
                "reason": "no credential registered",
            })
            return await tool_fn(*args, **kwargs)

        result = self._checker.check(record)

        if result.is_expired:
            self._blocks_total += 1
            if record.refresh_token:
                try:
                    record = await self._refresher.refresh(record)
                    self._store.put(record, service_name)
                    self._refreshes_triggered += 1
                    self._audit({
                        "event": "credential_refreshed",
                        "service": service_name,
                        "credential_id": record.credential_id,
                    })
                except CredentialRefreshError as exc:
                    self._audit({
                        "event": "credential_refresh_failed",
                        "service": service_name,
                        "error": exc.reason,
                    })
                    raise
            else:
                self._audit({
                    "event": "credential_expired_no_refresh",
                    "service": service_name,
                    "credential_id": record.credential_id,
                })
                raise CredentialExpiredError(service_name, record.credential_id)

        elif result.needs_refresh:
            try:
                record = await self._refresher.refresh(record)
                self._store.put(record, service_name)
                self._refreshes_triggered += 1
                self._audit({
                    "event": "credential_proactive_refresh",
                    "service": service_name,
                    "seconds_remaining": result.seconds_until_expiry,
                })
            except CredentialRefreshError:
                pass  # proactive refresh failure is non-fatal; use existing token

        return await tool_fn(*args, **kwargs)

    def stats(self) -> dict:
        return {
            "checks_total": self._checks_total,
            "refreshes_triggered": self._refreshes_triggered,
            "blocks_total": self._blocks_total,
        }


class CredentialExpiredError(Exception):
    def __init__(self, service_name: str, credential_id: str):
        super().__init__(
            f"credential '{credential_id}' for service '{service_name}' is expired "
            "and no refresh token is available"
        )
        self.service_name = service_name
        self.credential_id = credential_id
```

## Solution 6: Credential Health Dashboard

```python
import time
from typing import List


class CredentialHealthDashboard:
    """
    Produces a snapshot of all registered credentials with expiry
    status, refresh availability, and guard statistics for on-call visibility.
    """

    def __init__(
        self,
        store: CredentialStore,
        checker: CredentialExpiryChecker,
        guard: PreExecutionCredentialGuard,
    ):
        self._store = store
        self._checker = checker
        self._guard = guard

    def render(self) -> dict:
        expiring_soon = self._store.all_expiring_within(300.0)
        credential_status = []
        for record in expiring_soon:
            check = self._checker.check(record)
            credential_status.append({
                "credential_id": record.credential_id,
                "type": record.credential_type.value,
                "is_expired": check.is_expired,
                "seconds_until_expiry": check.seconds_until_expiry,
                "needs_refresh": check.needs_refresh,
                "has_refresh_token": record.refresh_token is not None,
                "recommendation": check.recommendation,
            })

        return {
            "generated_at": time.time(),
            "credentials_expiring_within_5m": len(expiring_soon),
            "credential_details": credential_status,
            "guard_stats": self._guard.stats(),
        }
```

## Comparison

| Approach | Expiry Detection | Proactive Refresh | Concurrent Refresh Safety | Pre-Call Blocking | Audit Trail |
|---|---|---|---|---|---|
| CredentialExpiryChecker | Yes (clock skew aware) | Yes (threshold) | No | No | No |
| CredentialRefresher | No | No | Yes (per-credential lock) | No | No |
| PreExecutionCredentialGuard | Via checker | Via refresher | Via refresher | Yes | Yes |
| CredentialStore | No | No | Thread-safe | No | No |
| CredentialHealthDashboard | No | No | No | No | Yes (snapshot) |

**Best for production**: Register a proactive refresh threshold at 2× the maximum tool call duration (e.g., 120 seconds for tools that can take up to 60 seconds) — this ensures a refresh completes before the token expires mid-call. For OAuth flows, always store the refresh token alongside the access token so that `PreExecutionCredentialGuard` can recover from expiry automatically rather than blocking. Emit `credential_expired_no_refresh` as a high-severity alert: it means the agent is permanently blocked until a human re-authenticates, and on-call should know immediately.
