---
title: "Agent Doesn't Implement Just-in-Time Secret Injection for Tool Calls"
description: "Agents that load all credentials at startup and pass them statically to tools expose long-lived secrets: they appear in memory dumps, process environment variables, log lines, and context windows. Implement just-in-time (JIT) secret injection — fetch credentials from a vault immediately before use, bind them to the specific tool invocation scope, and revoke or expire them automatically after the call completes."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-just-in-time-secret-injection-for-tool-calls
tags: [jit-secrets, secret-injection, vault-integration, credential-lifecycle, least-privilege, zero-trust]
symptoms:
  - "API keys appear in agent context window because tools receive them as string arguments"
  - "Credentials loaded at startup persist in memory for the entire process lifetime"
  - "Same long-lived secret used for every tool invocation — no per-call revocation possible"
  - "Secret rotation requires agent restart because credentials are cached at startup"
  - "Audit log shows tool call arguments including plaintext secrets"
---

## Why This Happens

The pattern of loading secrets from environment variables or a config file at startup is simple but insecure: credentials live in memory indefinitely, rotate only on restart, and are easily exposed in tracebacks, context dumps, or accidental logging of tool arguments. JIT injection moves credential retrieval to the moment of use: a vault client fetches a short-lived token, the tool executes, and the token is either revoked or allowed to expire on its own TTL. The secret window — the time between creation and expiry — shrinks from hours to seconds.

## Solution 1: Secret Lease

```python
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class SecretLease:
    """
    A single short-lived credential bound to one tool invocation.
    Expires after ttl_seconds and is optionally revocable via lease_id.
    """
    lease_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    secret_name: str = ""
    secret_value: str = ""
    tool_name: str = ""
    session_id: str = ""
    issued_at: float = field(default_factory=time.time)
    ttl_seconds: float = 30.0
    revoked: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_valid(self) -> bool:
        if self.revoked:
            return False
        return time.time() - self.issued_at < self.ttl_seconds

    def age_seconds(self) -> float:
        return time.time() - self.issued_at

    def remaining_seconds(self) -> float:
        return max(0.0, self.ttl_seconds - self.age_seconds())

    def redacted_value(self) -> str:
        if not self.secret_value:
            return ""
        return self.secret_value[:4] + "****"
```

## Solution 2: Vault Client Interface

```python
import time
from abc import ABC, abstractmethod
from typing import Dict, Optional


class VaultClient(ABC):
    """
    Abstract interface for secret retrieval.
    Implementations: HashiCorp Vault, AWS Secrets Manager,
    GCP Secret Manager, Azure Key Vault, or a local mock.
    """

    @abstractmethod
    async def fetch_secret(
        self, secret_name: str, ttl_seconds: float = 30.0
    ) -> SecretLease:
        ...

    @abstractmethod
    async def revoke_lease(self, lease_id: str) -> bool:
        ...


class InMemoryMockVaultClient(VaultClient):
    """
    Mock vault for testing — stores secrets in memory,
    issues leases, and supports revocation.
    """

    def __init__(self):
        self._secrets: Dict[str, str] = {}
        self._leases: Dict[str, SecretLease] = {}

    def register_secret(self, name: str, value: str) -> None:
        self._secrets[name] = value

    async def fetch_secret(
        self, secret_name: str, ttl_seconds: float = 30.0
    ) -> SecretLease:
        value = self._secrets.get(secret_name)
        if value is None:
            raise KeyError(f"secret '{secret_name}' not found in vault")
        lease = SecretLease(
            secret_name=secret_name,
            secret_value=value,
            ttl_seconds=ttl_seconds,
        )
        self._leases[lease.lease_id] = lease
        return lease

    async def revoke_lease(self, lease_id: str) -> bool:
        lease = self._leases.get(lease_id)
        if lease:
            lease.revoked = True
            return True
        return False
```

## Solution 3: JIT Secret Injector

```python
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Dict, Optional


class JITSecretInjector:
    """
    Fetches secrets immediately before tool execution and revokes them after.
    Provides an async context manager that yields the lease — the secret
    is valid only for the duration of the `async with` block.
    Caches leases within a short reuse window to avoid vault round-trips
    for the same secret within a single tool batch.
    """

    def __init__(
        self,
        vault: VaultClient,
        default_ttl_seconds: float = 30.0,
        reuse_window_seconds: float = 5.0,
        auto_revoke: bool = True,
    ):
        self._vault = vault
        self._default_ttl = default_ttl_seconds
        self._reuse_window = reuse_window_seconds
        self._auto_revoke = auto_revoke
        self._lease_cache: Dict[str, SecretLease] = {}
        self._fetch_count = 0
        self._revoke_count = 0
        self._cache_hits = 0

    @asynccontextmanager
    async def inject(
        self,
        secret_name: str,
        tool_name: str = "",
        session_id: str = "",
        ttl_seconds: Optional[float] = None,
    ) -> AsyncIterator[SecretLease]:
        ttl = ttl_seconds or self._default_ttl
        cache_key = f"{secret_name}:{session_id}"

        # Try reuse from cache (within reuse window)
        cached = self._lease_cache.get(cache_key)
        reusing = False
        if cached and cached.is_valid() and cached.remaining_seconds() > self._reuse_window:
            lease = cached
            reusing = True
            self._cache_hits += 1
        else:
            lease = await self._vault.fetch_secret(secret_name, ttl_seconds=ttl)
            lease.tool_name = tool_name
            lease.session_id = session_id
            self._lease_cache[cache_key] = lease
            self._fetch_count += 1

        try:
            yield lease
        finally:
            if not reusing and self._auto_revoke:
                try:
                    await self._vault.revoke_lease(lease.lease_id)
                    self._revoke_count += 1
                except Exception:
                    pass
            self._lease_cache.pop(cache_key, None)

    def stats(self) -> dict:
        return {
            "total_fetches": self._fetch_count,
            "total_revocations": self._revoke_count,
            "cache_hits": self._cache_hits,
        }
```

## Solution 4: Tool Call Secret Binder

```python
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


@dataclass
class SecretBinding:
    """Maps a tool argument name to the vault secret name it should receive."""
    arg_name: str
    secret_name: str
    ttl_seconds: float = 30.0


@dataclass
class SecretBindingRegistry:
    """Declares which tool arguments are secrets and where to fetch them from."""
    bindings: Dict[str, List[SecretBinding]] = field(default_factory=dict)

    def register(self, tool_name: str, binding: SecretBinding) -> None:
        self._bindings_for(tool_name).append(binding)

    def _bindings_for(self, tool_name: str) -> List[SecretBinding]:
        if tool_name not in self.bindings:
            self.bindings[tool_name] = []
        return self.bindings[tool_name]

    def get_bindings(self, tool_name: str) -> List[SecretBinding]:
        return self.bindings.get(tool_name, [])


class ToolCallSecretBinder:
    """
    Before dispatching a tool call, replaces secret argument placeholders
    with JIT-fetched lease values. After the call, revokes all leases.
    Argument values of the form "__secret:vault_key__" are substituted.
    """

    PLACEHOLDER_PREFIX = "__secret:"
    PLACEHOLDER_SUFFIX = "__"

    def __init__(self, injector: JITSecretInjector):
        self._injector = injector

    async def bind_and_call(
        self,
        tool_fn: Callable,
        tool_name: str,
        args: Dict[str, Any],
        session_id: str = "",
    ) -> Any:
        leases = []
        bound_args = dict(args)

        for arg_name, value in args.items():
            if (
                isinstance(value, str)
                and value.startswith(self.PLACEHOLDER_PREFIX)
                and value.endswith(self.PLACEHOLDER_SUFFIX)
            ):
                secret_name = value[len(self.PLACEHOLDER_PREFIX):-len(self.PLACEHOLDER_SUFFIX)]
                lease = await self._injector._vault.fetch_secret(secret_name)
                leases.append(lease)
                bound_args[arg_name] = lease.secret_value

        try:
            return await tool_fn(**bound_args)
        finally:
            for lease in leases:
                await self._injector._vault.revoke_lease(lease.lease_id)
```

## Solution 5: Secret Exposure Detector

```python
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import time


@dataclass
class ExposureEvent:
    location: str       # "tool_args" | "context" | "log_line"
    secret_name: str
    redacted_value: str
    detected_at: float = field(default_factory=time.time)


class SecretExposureDetector:
    """
    Scans tool arguments, log lines, and context snippets for known
    secret patterns. Fires exposure events when secrets are detected
    in unexpected locations — indicating JIT injection is being bypassed.
    """

    SECRET_PATTERNS = [
        (r"sk-[a-zA-Z0-9]{20,}", "openai_key"),
        (r"AKIA[0-9A-Z]{16}", "aws_access_key"),
        (r"(?i)bearer\s+[a-zA-Z0-9\-_\.]{20,}", "bearer_token"),
        (r"(?i)(password|secret|api_key)\s*[:=]\s*['\"]?[\w\-\.]{8,}", "generic_secret"),
    ]

    def __init__(self):
        self._compiled = [
            (re.compile(pattern), name) for pattern, name in self.SECRET_PATTERNS
        ]
        self._exposures: List[ExposureEvent] = []

    def scan(self, text: str, location: str) -> List[ExposureEvent]:
        new_events = []
        for pattern, name in self._compiled:
            matches = pattern.findall(text)
            for match in matches:
                redacted = match[:6] + "****"
                event = ExposureEvent(
                    location=location,
                    secret_name=name,
                    redacted_value=redacted,
                )
                new_events.append(event)
                self._exposures.append(event)
        return new_events

    def scan_args(self, tool_name: str, args: Dict[str, Any]) -> List[ExposureEvent]:
        text = str(args)
        return self.scan(text, f"tool_args:{tool_name}")

    def recent_exposures(self, hours: float = 1.0) -> List[ExposureEvent]:
        cutoff = time.time() - hours * 3600
        return [e for e in self._exposures if e.detected_at >= cutoff]

    def summary(self) -> dict:
        recent = self.recent_exposures(1.0)
        return {
            "exposures_last_hour": len(recent),
            "by_location": {
                loc: sum(1 for e in recent if e.location.startswith(loc))
                for loc in {"tool_args", "context", "log_line"}
            },
        }
```

## Solution 6: JIT Secret Health Monitor

```python
import time


class JITSecretHealthMonitor:
    """
    Monitors JIT injection efficiency and exposure detector state.
    Alerts when auto-revocation is failing or secrets are being exposed.
    """

    def __init__(
        self,
        injector: JITSecretInjector,
        detector: SecretExposureDetector,
        max_exposure_rate: float = 0.0,   # zero tolerance for exposures
    ):
        self._injector = injector
        self._detector = detector
        self._max_exposure_rate = max_exposure_rate

    def check(self) -> dict:
        inj_stats = self._injector.stats()
        det_summary = self._detector.summary()
        alerts = []

        revocation_rate = inj_stats["total_revocations"] / max(
            inj_stats["total_fetches"], 1
        )
        if inj_stats["total_fetches"] > 10 and revocation_rate < 0.95:
            alerts.append({
                "type": "low_revocation_rate",
                "revocation_rate": round(revocation_rate, 4),
                "recommendation": "check vault connectivity and auto_revoke setting",
            })

        if det_summary["exposures_last_hour"] > 0:
            alerts.append({
                "type": "secret_exposure_detected",
                "count": det_summary["exposures_last_hour"],
                "by_location": det_summary["by_location"],
                "recommendation": "audit tool call logging and context window content",
            })

        return {
            "generated_at": time.time(),
            "healthy": len(alerts) == 0,
            "injection_stats": inj_stats,
            "exposure_summary": det_summary,
            "alerts": alerts,
        }
```

## Comparison

| Approach | JIT Fetch | Auto Revoke | Arg Substitution | Exposure Detection |
|---|---|---|---|---|
| JITSecretInjector | Yes (context mgr) | Yes (on exit) | No | No |
| ToolCallSecretBinder | Via injector | Yes | Yes (placeholder) | No |
| SecretExposureDetector | No | No | No | Yes (regex scan) |
| JITSecretHealthMonitor | No | No | No | Via detector |

**Best for production**: Use `JITSecretInjector` as an `async with` context manager wrapping every tool call that needs credentials — never pass the lease object outside the block. Set `ttl_seconds=30` for API keys and `ttl_seconds=10` for database passwords. Enable `SecretExposureDetector.scan_args()` before every tool dispatch — scan cost is negligible and it catches JIT bypass bugs early. Run `JITSecretHealthMonitor.check()` every 5 minutes; a low revocation rate usually means the vault is unreachable, which is itself a security event.
