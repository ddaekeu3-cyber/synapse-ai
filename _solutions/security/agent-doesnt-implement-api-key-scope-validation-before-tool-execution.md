---
title: "Agent Doesn't Implement API Key Scope Validation Before Tool Execution"
description: "Agents that use a single API key with broad permissions for all tool calls violate least-privilege: a tool that only needs read access uses a key that also has write and delete permissions, so a compromised tool or prompt injection can escalate to destructive operations. Implement API key scope validation that verifies the key provided to each tool has only the permissions that tool requires."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-api-key-scope-validation-before-tool-execution
tags: [api-key-scoping, least-privilege, permission-validation, key-management, tool-security, access-control]
symptoms:
  - "A single API key with admin permissions is used for all tool calls"
  - "Read-only tools receive keys that also have write and delete access"
  - "No validation that a key has the required scope before invoking a tool"
  - "Prompt injection that pivots to a write operation succeeds because the key allows it"
  - "Cannot audit which tools used which permission levels"
---

## Why This Happens

Managing multiple API keys with different permission levels is operationally more complex than using one key for everything. A single admin key works for every tool, requires no scope management, and eliminates key rotation complexity. The security cost — a single compromised tool or prompt injection gaining full API access — is accepted implicitly. Scope validation requires defining what permissions each tool needs, assigning appropriately-scoped keys, and enforcing the match at dispatch time.

## Solution 1: API Key Scope Definition

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Optional, Set


class APIPermission(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"
    LIST = "list"
    EXECUTE = "execute"
    PUBLISH = "publish"
    SUBSCRIBE = "subscribe"


@dataclass(frozen=True)
class APIKeyScope:
    """Defines the permissions granted by an API key."""
    key_id: str
    service: str               # e.g., "github", "stripe", "s3"
    permissions: FrozenSet[APIPermission]
    resource_patterns: FrozenSet[str] = frozenset()  # e.g., "repo:read:*"
    environment: str = "production"  # production | staging | sandbox

    def has_permission(self, permission: APIPermission) -> bool:
        if APIPermission.ADMIN in self.permissions:
            return True
        return permission in self.permissions

    def has_all_permissions(self, required: Set[APIPermission]) -> bool:
        return all(self.has_permission(p) for p in required)

    def is_broader_than_needed(self, required: Set[APIPermission]) -> bool:
        """True if key grants more permissions than the tool needs."""
        if APIPermission.ADMIN in self.permissions and APIPermission.ADMIN not in required:
            return True
        extra = set(self.permissions) - required
        dangerous_extras = {APIPermission.DELETE, APIPermission.ADMIN, APIPermission.WRITE}
        return bool(extra & dangerous_extras)


@dataclass
class ToolPermissionRequirement:
    """Declares what permissions a tool needs to execute."""
    tool_name: str
    service: str
    required_permissions: Set[APIPermission]
    description: str = ""
    allow_elevated: bool = False   # if True, warn but don't block overly-scoped keys
```

## Solution 2: API Key Registry

```python
import hashlib
import os
from typing import Dict, List, Optional


class APIKeyRegistry:
    """
    Manages scoped API keys for different services and permission levels.
    Keys are referenced by key_id — the actual key bytes are never stored in plain sight.
    """

    def __init__(self):
        self._scopes: Dict[str, APIKeyScope] = {}
        self._key_values: Dict[str, str] = {}   # key_id -> actual key (in-memory only)

    def register(self, scope: APIKeyScope, key_value: str) -> None:
        self._scopes[scope.key_id] = scope
        self._key_values[scope.key_id] = key_value

    def get_scope(self, key_id: str) -> Optional[APIKeyScope]:
        return self._scopes.get(key_id)

    def get_key_value(self, key_id: str) -> Optional[str]:
        return self._key_values.get(key_id)

    def find_key_for_tool(
        self,
        service: str,
        required_permissions: set,
        prefer_minimal_scope: bool = True,
    ) -> Optional[APIKeyScope]:
        """Find the least-privileged key that satisfies the tool's requirements."""
        candidates = [
            scope for scope in self._scopes.values()
            if scope.service == service and scope.has_all_permissions(required_permissions)
        ]
        if not candidates:
            return None
        if prefer_minimal_scope:
            return min(candidates, key=lambda s: len(s.permissions))
        return candidates[0]

    def keys_for_service(self, service: str) -> List[APIKeyScope]:
        return [s for s in self._scopes.values() if s.service == service]
```

## Solution 3: Scope Validator

```python
from typing import Optional


class APIKeyScopeValidator:
    """
    Validates that a key provided to a tool has the required permissions
    and is not overly broad for the operation.
    """

    def __init__(self, registry: APIKeyRegistry):
        self._registry = registry
        self._validation_log = []

    def validate(
        self,
        key_id: str,
        requirement: ToolPermissionRequirement,
    ) -> dict:
        scope = self._registry.get_scope(key_id)

        if scope is None:
            return self._result(False, "unknown_key", f"key_id '{key_id}' not registered")

        if scope.service != requirement.service:
            return self._result(
                False, "wrong_service",
                f"key is for service '{scope.service}', tool requires '{requirement.service}'"
            )

        missing = {
            p for p in requirement.required_permissions
            if not scope.has_permission(p)
        }
        if missing:
            return self._result(
                False, "insufficient_permissions",
                f"key missing permissions: {[p.value for p in missing]}"
            )

        overly_broad = scope.is_broader_than_needed(requirement.required_permissions)
        if overly_broad and not requirement.allow_elevated:
            return self._result(
                False, "overly_broad_key",
                f"key has excess permissions beyond what tool '{requirement.tool_name}' needs"
            )

        status = "warn_elevated" if overly_broad else "ok"
        return self._result(True, status, "")

    def _result(self, valid: bool, reason: str, detail: str) -> dict:
        import time
        entry = {"valid": valid, "reason": reason, "detail": detail, "ts": time.time()}
        self._validation_log.append(entry)
        if len(self._validation_log) > 10000:
            self._validation_log.pop(0)
        return entry

    def recent_failures(self, n: int = 20) -> list:
        return [e for e in self._validation_log[-100:] if not e["valid"]][-n:]
```

## Solution 4: Scope-Enforced Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class ScopeEnforcedToolDispatcher:
    """
    Dispatches tool calls only after validating that the provided API key
    has the required scope. Automatically selects the least-privileged
    available key if no key_id is specified.
    """

    def __init__(
        self,
        validator: APIKeyScopeValidator,
        registry: APIKeyRegistry,
        requirements: Dict[str, ToolPermissionRequirement],
    ):
        self._validator = validator
        self._registry = registry
        self._requirements = requirements
        self._blocked_count = 0
        self._dispatched_count = 0

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        fn: Callable,
        key_id: Optional[str] = None,
    ) -> Any:
        requirement = self._requirements.get(tool_name)
        if requirement is None:
            raise ValueError(f"no permission requirement defined for tool '{tool_name}'")

        # Auto-select minimal key if not specified
        if key_id is None:
            scope = self._registry.find_key_for_tool(
                service=requirement.service,
                required_permissions=requirement.required_permissions,
            )
            if scope is None:
                self._blocked_count += 1
                raise ScopeValidationError(
                    tool_name, "no_suitable_key",
                    f"no key available for service '{requirement.service}' with required permissions"
                )
            key_id = scope.key_id

        result = self._validator.validate(key_id, requirement)
        if not result["valid"]:
            self._blocked_count += 1
            raise ScopeValidationError(tool_name, result["reason"], result["detail"])

        # Inject key value into args
        key_value = self._registry.get_key_value(key_id)
        if key_value:
            args = {**args, "_api_key": key_value}

        self._dispatched_count += 1
        return await fn(**args)

    def stats(self) -> dict:
        return {
            "dispatched": self._dispatched_count,
            "blocked": self._blocked_count,
            "block_rate": round(self._blocked_count / max(self._dispatched_count + self._blocked_count, 1), 4),
        }


class ScopeValidationError(Exception):
    def __init__(self, tool_name: str, reason: str, detail: str):
        super().__init__(f"scope validation failed for tool '{tool_name}' [{reason}]: {detail}")
        self.tool_name = tool_name
        self.reason = reason
```

## Solution 5: Key Rotation Coordinator

```python
import time
from typing import Callable, List, Optional


class ScopedKeyRotationCoordinator:
    """
    Coordinates rotation of scoped API keys without service interruption.
    Validates new keys before retiring old ones.
    """

    def __init__(self, registry: APIKeyRegistry, validator: APIKeyScopeValidator):
        self._registry = registry
        self._validator = validator
        self._rotation_log: List[dict] = []

    async def rotate_key(
        self,
        key_id: str,
        new_key_value: str,
        validate_fn: Optional[Callable] = None,
    ) -> dict:
        scope = self._registry.get_scope(key_id)
        if scope is None:
            return {"success": False, "error": f"key_id '{key_id}' not found"}

        # Optionally validate the new key works before replacing
        if validate_fn:
            try:
                await validate_fn(new_key_value)
            except Exception as exc:
                return {"success": False, "error": f"new key validation failed: {exc}"}

        self._registry._key_values[key_id] = new_key_value
        self._rotation_log.append({
            "ts": time.time(),
            "key_id": key_id,
            "service": scope.service,
            "success": True,
        })
        return {"success": True, "key_id": key_id, "service": scope.service}

    def rotation_history(self) -> List[dict]:
        return list(self._rotation_log)
```

## Solution 6: Scope Security Dashboard

```python
import time


class APIScopeDashboard:
    """
    Combines dispatcher stats, validation failures, and key registry
    status into a least-privilege health view.
    """

    def __init__(
        self,
        dispatcher: ScopeEnforcedToolDispatcher,
        validator: APIKeyScopeValidator,
        registry: APIKeyRegistry,
    ):
        self._dispatcher = dispatcher
        self._validator = validator
        self._registry = registry

    def render(self) -> dict:
        stats = self._dispatcher.stats()
        failures = self._validator.recent_failures(n=10)
        admin_keys = [
            s.key_id for s in self._registry._scopes.values()
            if "admin" in [p.value for p in s.permissions]
        ]

        return {
            "generated_at": time.time(),
            "dispatcher_stats": stats,
            "recent_failures": failures,
            "admin_key_count": len(admin_keys),
            "total_registered_keys": len(self._registry._scopes),
            "alert": len(failures) > 0 or stats["block_rate"] > 0.01,
        }
```

## Comparison

| Approach | Scope Definition | Key Auto-Select | Excess Permission Block | Rotation | Dashboard |
|---|---|---|---|---|---|
| APIKeyScope | Yes | No | Via is_broader_than_needed | No | No |
| APIKeyRegistry | No | Yes (minimal) | No | No | No |
| APIKeyScopeValidator | No | No | Yes | No | No |
| ScopeEnforcedToolDispatcher | Via requirements | Via registry | Via validator | No | No |
| ScopedKeyRotationCoordinator | No | No | No | Yes | No |
| APIScopeDashboard | No | No | No | No | Yes |

**Best for production**: Issue separate API keys for read-only tools (search, lookup, fetch) and write tools (create, update, delete) — never share keys across trust boundaries. Set `allow_elevated=False` for all delete and write tools to block overly-broad keys; set it to `True` only for admin operations that genuinely need broad access. Store key values in a secrets manager and load them at runtime via `registry.register()` — never commit key values to code. Alert on any `overly_broad_key` validation failure: it means a write key is being used for a read tool, which violates least-privilege and should be investigated.
