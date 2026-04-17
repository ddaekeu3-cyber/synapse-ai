---
title: "Agent Doesn't Implement Tool Permission Scoping Per User Role"
description: "Agents that expose the full tool catalog to every user regardless of role allow unprivileged users to invoke administrative tools, write tools, or data-exfiltration tools they should never see. Implement tool permission scoping that maps roles to allowed tool sets, enforces the scope at invocation time, and prevents the LLM from even receiving schema definitions for tools the requesting user is not authorized to use."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-permission-scoping-per-user-role
tags: [tool-permissions, rbac, role-based-access, tool-scoping, authorization, least-privilege]
symptoms:
  - "All users see the same tool list regardless of their role or privilege level"
  - "An anonymous or read-only user can invoke a tool that writes to the database"
  - "The LLM receives schema definitions for admin tools even when serving unauthenticated requests"
  - "No audit trail of which user invoked which tool with what arguments"
  - "Removing a tool from a user's allowed set requires a code change rather than a config update"
---

## Why This Happens

Tool schemas are typically defined at agent initialization as a static list passed to the LLM. There is no per-request filtering step that removes tools the current user is not authorized to use. The LLM then selects from the full catalog, and the dispatcher executes whatever the model chose. Even if the dispatcher theoretically checks permissions, the LLM still generates unauthorized tool calls that waste tokens and create audit noise. The fix is to filter the tool schema list before it reaches the LLM — if the model never sees the tool, it cannot invoke it.

## Solution 1: Tool Permission Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Set


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"
    ADMIN = "admin"
    DESTRUCTIVE = "destructive"
    EXTERNAL_CALL = "external_call"


@dataclass(frozen=True)
class ToolPermission:
    tool_name: str
    risk_level: ToolRisk
    required_roles: FrozenSet[str]      # any of these roles grants access
    required_scopes: FrozenSet[str]     # all of these scopes must be present
    allow_anonymous: bool = False
    description: str = ""

    def is_allowed_for(self, roles: Set[str], scopes: Set[str], anonymous: bool = False) -> bool:
        if self.allow_anonymous:
            return True
        if anonymous:
            return False
        has_role = bool(self.required_roles & roles) or not self.required_roles
        has_scopes = self.required_scopes.issubset(scopes)
        return has_role and has_scopes
```

## Solution 2: Tool Permission Registry

```python
from typing import Dict, List, Optional, Set


class ToolPermissionRegistry:
    """
    Stores permission descriptors for all registered tools.
    Tools not registered default to ADMIN risk with no anonymous access.
    """

    def __init__(self):
        self._permissions: Dict[str, ToolPermission] = {}

    def register(self, permission: ToolPermission) -> None:
        self._permissions[permission.tool_name] = permission

    def get(self, tool_name: str) -> ToolPermission:
        return self._permissions.get(
            tool_name,
            ToolPermission(
                tool_name=tool_name,
                risk_level=ToolRisk.ADMIN,
                required_roles=frozenset({"admin"}),
                required_scopes=frozenset(),
                allow_anonymous=False,
                description="unregistered tool — defaults to admin-only",
            ),
        )

    def allowed_tools(
        self,
        roles: Set[str],
        scopes: Set[str],
        anonymous: bool = False,
    ) -> List[str]:
        return [
            name
            for name, perm in self._permissions.items()
            if perm.is_allowed_for(roles, scopes, anonymous)
        ]

    def all_tool_names(self) -> List[str]:
        return list(self._permissions.keys())
```

## Solution 3: Request Identity

```python
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Set


@dataclass
class RequestIdentity:
    user_id: str
    roles: FrozenSet[str]
    scopes: FrozenSet[str]
    anonymous: bool = False
    session_id: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def anonymous_user(cls) -> "RequestIdentity":
        return cls(
            user_id="",
            roles=frozenset(),
            scopes=frozenset(),
            anonymous=True,
        )

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes
```

## Solution 4: Scoped Tool Schema Filter

```python
from typing import Any, Dict, List


class ScopedToolSchemaFilter:
    """
    Filters a full tool schema list to only those the requesting identity
    is authorized to use. The filtered list is passed to the LLM — tools
    the user cannot use are never exposed to the model.
    """

    def __init__(self, registry: ToolPermissionRegistry):
        self._registry = registry

    def filter_schemas(
        self,
        all_schemas: List[Dict[str, Any]],
        identity: RequestIdentity,
    ) -> List[Dict[str, Any]]:
        allowed = set(
            self._registry.allowed_tools(
                roles=set(identity.roles),
                scopes=set(identity.scopes),
                anonymous=identity.anonymous,
            )
        )
        return [
            schema for schema in all_schemas
            if schema.get("name") in allowed
        ]

    def denied_tools(
        self,
        all_schemas: List[Dict[str, Any]],
        identity: RequestIdentity,
    ) -> List[str]:
        allowed = set(
            self._registry.allowed_tools(
                roles=set(identity.roles),
                scopes=set(identity.scopes),
                anonymous=identity.anonymous,
            )
        )
        return [
            schema["name"] for schema in all_schemas
            if schema.get("name") not in allowed
        ]
```

## Solution 5: Tool Invocation Authorizer

```python
import time
from typing import Any, Dict, Optional


class ToolInvocationAuthorizer:
    """
    Secondary enforcement gate at dispatch time. Even if a tool call somehow
    reaches the dispatcher with an unauthorized tool name, this blocks execution.
    Acts as defense-in-depth after schema filtering.
    """

    def __init__(
        self,
        registry: ToolPermissionRegistry,
        audit_logger: "ToolAuthorizationAuditLogger",
    ):
        self._registry = registry
        self._logger = audit_logger

    def authorize(
        self,
        tool_name: str,
        identity: RequestIdentity,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> bool:
        permission = self._registry.get(tool_name)
        allowed = permission.is_allowed_for(
            roles=set(identity.roles),
            scopes=set(identity.scopes),
            anonymous=identity.anonymous,
        )
        self._logger.record(
            tool_name=tool_name,
            identity=identity,
            allowed=allowed,
            risk_level=permission.risk_level,
        )
        return allowed

    def enforce(
        self,
        tool_name: str,
        identity: RequestIdentity,
        tool_args: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.authorize(tool_name, identity, tool_args):
            raise ToolAuthorizationError(tool_name, identity.user_id, set(identity.roles))


class ToolAuthorizationError(Exception):
    def __init__(self, tool_name: str, user_id: str, roles: set):
        super().__init__(
            f"user '{user_id}' (roles={roles}) not authorized to invoke tool '{tool_name}'"
        )
        self.tool_name = tool_name
        self.user_id = user_id
```

## Solution 6: Tool Authorization Audit Logger

```python
import time
from typing import List


class ToolAuthorizationAuditLogger:
    """
    Records all tool authorization decisions for compliance auditing.
    Surfaces denial patterns that may indicate misconfigured roles or attack attempts.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        tool_name: str,
        identity: RequestIdentity,
        allowed: bool,
        risk_level: ToolRisk,
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "user_id": identity.user_id,
            "session_id": identity.session_id,
            "roles": list(identity.roles),
            "allowed": allowed,
            "risk_level": risk_level.value,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        if not recent:
            return {"window_seconds": window_seconds, "decisions": 0}

        total = len(recent)
        denied = [r for r in recent if not r["allowed"]]
        denial_by_tool: dict = {}
        for r in denied:
            name = r["tool_name"]
            denial_by_tool[name] = denial_by_tool.get(name, 0) + 1

        return {
            "window_seconds": window_seconds,
            "decisions": total,
            "allowed": total - len(denied),
            "denied": len(denied),
            "denial_rate": round(len(denied) / total, 4),
            "top_denied_tools": sorted(
                denial_by_tool.items(), key=lambda kv: kv[1], reverse=True
            )[:5],
        }
```

## Comparison

| Approach | Schema-Level Filtering | Dispatch-Level Enforcement | Role + Scope Checks | Anonymous Handling | Audit Log |
|---|---|---|---|---|---|
| ToolPermission | Yes (is_allowed_for) | No | Yes (both) | Yes | No |
| ToolPermissionRegistry | Via permissions | No | Via permissions | Via permissions | No |
| ScopedToolSchemaFilter | Yes (pre-LLM) | No | Via registry | Via registry | No |
| ToolInvocationAuthorizer | No | Yes (defense-in-depth) | Via registry | Via registry | Via logger |
| ToolAuthorizationAuditLogger | No | No | No | No | Yes |

**Best for production**: Apply `ScopedToolSchemaFilter` before constructing the LLM request — never pass tool schemas to the model and rely solely on dispatch-time enforcement. If the LLM never sees a tool, it cannot hallucinate a call to it. Keep `ToolInvocationAuthorizer.enforce()` as defense-in-depth for cases where tool calls arrive from non-LLM paths (direct API calls, test harnesses). Use `required_scopes` for fine-grained access control within a role: a user with the `analyst` role might have `read:database` scope but not `write:database` scope, allowing a single role to span multiple permission levels without role proliferation. Monitor `denial_rate` via the audit logger: a spike in denials from a single user often indicates a confused LLM that received an overly broad system prompt, not a malicious actor.
