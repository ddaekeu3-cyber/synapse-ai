---
title: "Agent Doesn't Implement API Key Scope Validation at Runtime"
description: "Agents that use API keys without validating their actual granted scopes may silently succeed with over-privileged keys (security risk) or fail mid-task when a required scope is missing (reliability risk). Implement runtime API key scope validation to verify that keys have exactly the permissions required before any state-mutating operation begins — failing fast with a clear error rather than discovering missing scopes after partial execution."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-api-key-scope-validation-at-runtime
tags: [api-key-validation, scope-enforcement, least-privilege, pre-flight, security, authorization]
symptoms:
  - "Agent uses an admin API key for a read-only operation — scope is never validated or enforced"
  - "Task fails at step 7 because the API key was missing write scope — discovered only after side effects"
  - "No audit trail showing which scopes were actually used versus what the key allowed"
  - "Key rotation breaks tasks silently because the new key lacks a scope the old key had"
  - "Over-privileged keys used as a convenience — 'it works' without knowing what it can actually do"
---

## Why This Happens

API keys are typically validated only at first use — if the call succeeds, the key is assumed valid. But scope validation (what actions the key is authorized to perform) is separate from authentication validation (whether the key is recognized). Agents that don't enumerate and check key scopes before multi-step tasks discover scope gaps only at the point of failure, potentially after irreversible side effects. Runtime scope validation queries the authorization service for the key's actual granted scopes and checks them against the task's required scope set before the first mutation.

## Solution 1: Scope Manifest

```python
import time
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Set

@dataclass
class ScopeManifest:
    """Describes the scopes associated with an API key."""
    key_id: str                       # opaque identifier (not the key itself)
    key_type: str                     # "service_account" | "user_token" | "api_key"
    granted_scopes: FrozenSet[str]    # scopes the key actually has
    owner: str = ""
    environment: str = ""            # "production" | "staging" | "development"
    expires_at: Optional[float] = None
    fetched_at: float = field(default_factory=time.time)
    ttl_seconds: float = 300.0        # re-validate after this many seconds

    def has_scope(self, scope: str) -> bool:
        return scope in self.granted_scopes

    def has_all_scopes(self, required: Set[str]) -> bool:
        return required.issubset(self.granted_scopes)

    def missing_scopes(self, required: Set[str]) -> Set[str]:
        return required - self.granted_scopes

    def is_stale(self) -> bool:
        return time.time() - self.fetched_at > self.ttl_seconds

    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at

    def is_valid(self) -> bool:
        return not self.is_expired()
```

## Solution 2: Scope Resolver

```python
import asyncio
import hashlib
import time
from typing import Callable, Coroutine, Dict, Optional

class ScopeResolver:
    """
    Fetches and caches the scope manifest for an API key.
    Abstracts the provider-specific introspection endpoint (OAuth token info,
    AWS IAM, GCP IAM, or custom scope registry).
    Re-fetches when the cached manifest is stale.
    """

    def __init__(
        self,
        introspect_fn: Callable[[str], Coroutine[None, None, Dict]],
        cache_ttl_seconds: float = 300.0,
    ):
        """
        introspect_fn: async callable that takes a key and returns a dict with:
            {key_id, granted_scopes: list[str], owner, environment, expires_at}
        """
        self._introspect = introspect_fn
        self._cache_ttl = cache_ttl_seconds
        self._cache: Dict[str, ScopeManifest] = {}
        self._lock = asyncio.Lock()
        self._cache_hits = 0
        self._cache_misses = 0

    def _cache_key(self, api_key: str) -> str:
        return hashlib.sha256(api_key.encode()).hexdigest()[:16]

    async def resolve(self, api_key: str) -> ScopeManifest:
        ck = self._cache_key(api_key)
        async with self._lock:
            cached = self._cache.get(ck)
            if cached and not cached.is_stale() and cached.is_valid():
                self._cache_hits += 1
                return cached

        self._cache_misses += 1
        raw = await self._introspect(api_key)
        manifest = ScopeManifest(
            key_id=raw.get("key_id", ck),
            key_type=raw.get("key_type", "api_key"),
            granted_scopes=frozenset(raw.get("granted_scopes", [])),
            owner=raw.get("owner", ""),
            environment=raw.get("environment", ""),
            expires_at=raw.get("expires_at"),
            ttl_seconds=self._cache_ttl,
        )
        async with self._lock:
            self._cache[ck] = manifest
        return manifest

    def invalidate(self, api_key: str) -> None:
        ck = self._cache_key(api_key)
        self._cache.pop(ck, None)

    def stats(self) -> dict:
        total = self._cache_hits + self._cache_misses
        return {
            "cached_manifests": len(self._cache),
            "cache_hit_rate": round(self._cache_hits / max(total, 1), 4),
        }
```

## Solution 3: Pre-Task Scope Validator

```python
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

@dataclass
class ScopeValidationResult:
    key_id: str
    task_name: str
    required_scopes: Set[str]
    granted_scopes: frozenset
    missing_scopes: Set[str]
    excess_scopes: Set[str]   # granted but not required — flag for least-privilege audit
    passed: bool
    environment_match: bool
    key_expired: bool
    validation_time: float = field(default_factory=time.time)

class PreTaskScopeValidator:
    """
    Validates that an API key has all required scopes before a task starts.
    Also detects over-privilege (excess scopes) for least-privilege auditing.
    Fails fast before any state mutation if required scopes are missing.
    """

    def __init__(self, resolver: ScopeResolver, expected_environment: str = ""):
        self._resolver = resolver
        self._environment = expected_environment

    async def validate(
        self,
        api_key: str,
        task_name: str,
        required_scopes: Set[str],
    ) -> ScopeValidationResult:
        manifest = await self._resolver.resolve(api_key)
        missing = manifest.missing_scopes(required_scopes)
        excess = manifest.granted_scopes - required_scopes

        env_match = (
            not self._environment or
            manifest.environment == self._environment or
            manifest.environment == ""
        )

        passed = (
            len(missing) == 0 and
            env_match and
            manifest.is_valid()
        )

        return ScopeValidationResult(
            key_id=manifest.key_id,
            task_name=task_name,
            required_scopes=required_scopes,
            granted_scopes=manifest.granted_scopes,
            missing_scopes=missing,
            excess_scopes=excess,
            passed=passed,
            environment_match=env_match,
            key_expired=manifest.is_expired(),
        )

    async def assert_valid(
        self,
        api_key: str,
        task_name: str,
        required_scopes: Set[str],
    ) -> ScopeValidationResult:
        """Raises PermissionError if validation fails."""
        result = await self.validate(api_key, task_name, required_scopes)
        if not result.passed:
            reasons = []
            if result.missing_scopes:
                reasons.append(f"missing scopes: {sorted(result.missing_scopes)}")
            if result.key_expired:
                reasons.append("key expired")
            if not result.environment_match:
                reasons.append(f"wrong environment")
            raise PermissionError(
                f"API key scope validation failed for task '{task_name}': "
                + "; ".join(reasons)
            )
        return result
```

## Solution 4: Scope Usage Tracker

```python
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Set

@dataclass
class ScopeUsageRecord:
    key_id: str
    scope: str
    task_name: str
    tool_name: str
    timestamp: float

class ScopeUsageTracker:
    """
    Records which scopes are actually used per tool call.
    Enables least-privilege analysis: compare declared required scopes
    against actually-used scopes to identify over-declaration.
    """

    def __init__(self):
        self._records: List[ScopeUsageRecord] = []
        self._by_key: Dict[str, Set[str]] = defaultdict(set)
        self._by_tool: Dict[str, Set[str]] = defaultdict(set)

    def record_use(
        self,
        key_id: str,
        scope: str,
        task_name: str = "",
        tool_name: str = "",
    ) -> None:
        self._records.append(ScopeUsageRecord(
            key_id=key_id,
            scope=scope,
            task_name=task_name,
            tool_name=tool_name,
            timestamp=time.time(),
        ))
        self._by_key[key_id].add(scope)
        self._by_tool[tool_name].add(scope)

    def least_privilege_report(
        self,
        key_id: str,
        granted_scopes: Set[str],
    ) -> dict:
        used = self._by_key.get(key_id, set())
        unused = granted_scopes - used
        return {
            "key_id": key_id,
            "granted_count": len(granted_scopes),
            "used_count": len(used),
            "unused_scopes": sorted(unused),
            "over_privilege_ratio": round(len(unused) / max(len(granted_scopes), 1), 4),
        }

    def per_tool_scopes(self) -> Dict[str, List[str]]:
        return {tool: sorted(scopes) for tool, scopes in self._by_tool.items()}
```

## Solution 5: Scope Change Detector

```python
import asyncio
import time
from typing import Callable, Dict, FrozenSet, List, Optional

class ScopeChangeDetector:
    """
    Periodically re-resolves API key scopes to detect out-of-band changes.
    Fires alerts when scopes are added (escalation risk) or removed (task breakage risk).
    Run as a background task for keys used in long-running agent sessions.
    """

    def __init__(
        self,
        resolver: ScopeResolver,
        poll_interval_seconds: float = 300.0,
    ):
        self._resolver = resolver
        self._interval = poll_interval_seconds
        self._baseline: Dict[str, FrozenSet[str]] = {}
        self._change_handlers: List[Callable] = []
        self._running = False

    def add_change_handler(self, handler: Callable) -> None:
        self._change_handlers.append(handler)

    def set_baseline(self, api_key: str, scopes: FrozenSet[str]) -> None:
        import hashlib
        ck = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        self._baseline[ck] = scopes

    async def check_key(self, api_key: str) -> Optional[dict]:
        import hashlib
        ck = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        baseline = self._baseline.get(ck)
        if baseline is None:
            return None

        self._resolver.invalidate(api_key)
        manifest = await self._resolver.resolve(api_key)
        current = manifest.granted_scopes

        added = current - baseline
        removed = baseline - current
        if added or removed:
            change = {
                "key_id": manifest.key_id,
                "added_scopes": sorted(added),
                "removed_scopes": sorted(removed),
                "timestamp": time.time(),
            }
            self._baseline[ck] = current
            for handler in self._change_handlers:
                try:
                    handler(change)
                except Exception:
                    pass
            return change
        return None
```

## Solution 6: Scope Audit Dashboard

```python
import time
from typing import Dict, List

class ScopeAuditDashboard:
    """
    Aggregates scope validation results and usage data for security reporting.
    Surfaces: over-privileged keys, validation failures, scope drift events.
    """

    def __init__(
        self,
        validator: PreTaskScopeValidator,
        tracker: ScopeUsageTracker,
    ):
        self._validator = validator
        self._tracker = tracker
        self._validation_history: List[ScopeValidationResult] = []

    def record_validation(self, result: ScopeValidationResult) -> None:
        self._validation_history.append(result)

    def summary(self) -> dict:
        total = len(self._validation_history)
        failed = [r for r in self._validation_history if not r.passed]
        over_privileged = [
            r for r in self._validation_history
            if len(r.excess_scopes) > 2
        ]
        return {
            "total_validations": total,
            "passed": total - len(failed),
            "failed": len(failed),
            "pass_rate": round((total - len(failed)) / max(total, 1), 4),
            "over_privileged_cases": len(over_privileged),
            "failure_reasons": [
                {
                    "key_id": r.key_id,
                    "task": r.task_name,
                    "missing": sorted(r.missing_scopes),
                }
                for r in failed[:10]
            ],
            "per_tool_scope_usage": self._tracker.per_tool_scopes(),
        }
```

## Comparison

| Approach | Pre-Task Check | Caching | Drift Detection | Least-Privilege Audit |
|---|---|---|---|---|
| ScopeManifest | N/A (data model) | N/A | N/A | Via missing/excess |
| ScopeResolver | N/A (fetching) | Yes (TTL) | No | No |
| PreTaskScopeValidator | Yes | Via resolver | No | Yes (excess scopes) |
| ScopeUsageTracker | No | N/A | No | Yes (unused scopes) |
| ScopeChangeDetector | No | No | Yes (poll) | No |
| ScopeAuditDashboard | Via validator | N/A | N/A | Yes (reports) |

**Best for production**: Call `PreTaskScopeValidator.assert_valid()` before every multi-step task, failing fast before any mutations. Cache manifests for 5 minutes to avoid per-request introspection overhead. Run `ScopeChangeDetector` as a background task for keys used in sessions longer than the manifest TTL. Feed all validation results to `ScopeAuditDashboard` and alert on any key where `excess_scopes` count exceeds 3 — that's an over-privilege signal worth reviewing. Use `ScopeUsageTracker.least_privilege_report()` quarterly to identify keys that can have scopes removed.
