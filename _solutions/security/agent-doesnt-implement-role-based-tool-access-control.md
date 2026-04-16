---
title: "Agent Doesn't Implement Role-Based Tool Access Control"
description: "Agents that expose all tools to all callers violate the principle of least privilege: a customer-facing agent and an internal admin agent share the same tool catalog, meaning a compromised user session can access tools intended only for privileged operators. Implement role-based tool access control (RBAC) that assigns tools to roles, maps sessions to roles, and enforces access decisions at dispatch time before any tool logic executes."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-role-based-tool-access-control
tags: [rbac, tool-access-control, least-privilege, role-enforcement, tool-authorization, session-roles]
symptoms:
  - "Customer sessions can invoke admin tools like delete_user or export_all_data"
  - "All tools visible to the LLM regardless of caller privilege level"
  - "Tool catalog is the same for unauthenticated and authenticated callers"
  - "No session-level role assignment — every caller has full tool access"
  - "Privilege escalation via prompt injection reaches tools the user should not access"
---

## Why This Happens

Tool registries are built as flat catalogs without an access layer. Adding a new tool makes it available to all callers immediately. RBAC requires three things missing from most agent frameworks: a role definition (which tools belong to each role), a session-to-role mapping (which role does this session have), and enforcement at dispatch time. Without explicit enforcement, the LLM can be prompted to call any registered tool, regardless of whether the caller is authorized.

## Solution 1: Tool Permission Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, Set


class ToolPermission(str, Enum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    ADMIN = "admin"
    EXPORT = "export"


@dataclass(frozen=True)
class ToolPermissionDescriptor:
    tool_name: str
    required_permissions: FrozenSet[ToolPermission]
    min_role_level: int = 0      # 0=guest, 1=user, 2=operator, 3=admin
    description: str = ""

    def requires(self, permission: ToolPermission) -> bool:
        return permission in self.required_permissions
```

## Solution 2: Role Definition

```python
from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Set


@dataclass(frozen=True)
class Role:
    name: str
    level: int                                        # higher = more privileged
    granted_permissions: FrozenSet[ToolPermission]
    allowed_tools: FrozenSet[str] = field(default_factory=frozenset)
    # empty = permission-based; non-empty = explicit allowlist

    def can_use_tool(self, descriptor: ToolPermissionDescriptor) -> bool:
        if self.level < descriptor.min_role_level:
            return False
        if self.allowed_tools and descriptor.tool_name not in self.allowed_tools:
            return False
        return descriptor.required_permissions.issubset(self.granted_permissions)


_BUILT_IN_ROLES = {
    "guest": Role(
        name="guest",
        level=0,
        granted_permissions=frozenset({ToolPermission.READ}),
    ),
    "user": Role(
        name="user",
        level=1,
        granted_permissions=frozenset({ToolPermission.READ, ToolPermission.WRITE}),
    ),
    "operator": Role(
        name="operator",
        level=2,
        granted_permissions=frozenset({
            ToolPermission.READ, ToolPermission.WRITE,
            ToolPermission.DELETE, ToolPermission.EXPORT,
        }),
    ),
    "admin": Role(
        name="admin",
        level=3,
        granted_permissions=frozenset({
            ToolPermission.READ, ToolPermission.WRITE,
            ToolPermission.DELETE, ToolPermission.EXPORT,
            ToolPermission.ADMIN,
        }),
    ),
}
```

## Solution 3: RBAC Tool Registry

```python
from typing import Dict, List, Optional


class RBACToolRegistry:
    """
    Stores tool permission descriptors and provides role-aware
    tool catalog filtering for LLM system prompt construction.
    """

    def __init__(self):
        self._descriptors: Dict[str, ToolPermissionDescriptor] = {}

    def register(self, descriptor: ToolPermissionDescriptor) -> None:
        self._descriptors[descriptor.tool_name] = descriptor

    def allowed_tools(self, role: Role) -> List[str]:
        return [
            name for name, desc in self._descriptors.items()
            if role.can_use_tool(desc)
        ]

    def is_allowed(self, tool_name: str, role: Role) -> bool:
        desc = self._descriptors.get(tool_name)
        if desc is None:
            return False
        return role.can_use_tool(desc)

    def denial_reason(self, tool_name: str, role: Role) -> str:
        desc = self._descriptors.get(tool_name)
        if desc is None:
            return f"tool '{tool_name}' not registered"
        if role.level < desc.min_role_level:
            return f"role '{role.name}' (level {role.level}) below minimum level {desc.min_role_level}"
        missing = desc.required_permissions - role.granted_permissions
        return f"missing permissions: {', '.join(p.value for p in missing)}"
```

## Solution 4: Session Role Manager

```python
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Dict, Optional


@dataclass
class SessionRoleBinding:
    session_id: str
    role_name: str
    bound_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    identity: str = ""    # e.g., user_id or API key fingerprint

    def is_expired(self) -> bool:
        return self.expires_at is not None and time.time() > self.expires_at


class SessionRoleManager:
    """
    Maps session IDs to roles. Supports time-limited role bindings
    and default role assignment for unauthenticated sessions.
    """

    def __init__(self, default_role: str = "guest"):
        self._default_role = default_role
        self._bindings: Dict[str, SessionRoleBinding] = {}
        self._lock = Lock()

    def bind(
        self,
        session_id: str,
        role_name: str,
        identity: str = "",
        expires_in_seconds: Optional[float] = None,
    ) -> SessionRoleBinding:
        binding = SessionRoleBinding(
            session_id=session_id,
            role_name=role_name,
            identity=identity,
            expires_at=time.time() + expires_in_seconds if expires_in_seconds else None,
        )
        with self._lock:
            self._bindings[session_id] = binding
        return binding

    def get_role_name(self, session_id: str) -> str:
        with self._lock:
            binding = self._bindings.get(session_id)
        if binding is None or binding.is_expired():
            return self._default_role
        return binding.role_name

    def revoke(self, session_id: str) -> None:
        with self._lock:
            self._bindings.pop(session_id, None)
```

## Solution 5: RBAC-Enforced Tool Dispatcher

```python
import time
from typing import Any, Callable, Dict, Optional


class ToolAccessDeniedError(Exception):
    def __init__(self, tool_name: str, role_name: str, reason: str):
        super().__init__(f"access denied: '{role_name}' cannot call '{tool_name}': {reason}")
        self.tool_name = tool_name
        self.role_name = role_name
        self.reason = reason


class RBACToolDispatcher:
    """
    Enforces role-based access control on every tool dispatch.
    Logs denied attempts for security audit.
    """

    def __init__(
        self,
        registry: RBACToolRegistry,
        role_manager: SessionRoleManager,
        roles: Dict[str, Role],
        audit_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._registry = registry
        self._role_manager = role_manager
        self._roles = roles
        self._audit = audit_fn or (lambda _: None)
        self._denied_count = 0

    async def dispatch(
        self,
        session_id: str,
        tool_name: str,
        fn: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        role_name = self._role_manager.get_role_name(session_id)
        role = self._roles.get(role_name, self._roles.get("guest"))

        if not self._registry.is_allowed(tool_name, role):
            reason = self._registry.denial_reason(tool_name, role)
            self._denied_count += 1
            self._audit({
                "ts": time.time(),
                "event": "tool_access_denied",
                "session_id": session_id,
                "tool_name": tool_name,
                "role": role_name,
                "reason": reason,
            })
            raise ToolAccessDeniedError(tool_name, role_name, reason)

        return await fn(*args, **kwargs)

    def stats(self) -> dict:
        return {"denied_count": self._denied_count}
```

## Solution 6: RBAC Coverage Auditor

```python
from typing import Dict, List


class RBACCoverageAuditor:
    """
    Audits the RBAC configuration for coverage gaps:
    - Tools registered without permission descriptors
    - Roles with no tools (likely misconfigured)
    - Tools accessible to guest role that look sensitive
    """

    def __init__(
        self,
        registry: RBACToolRegistry,
        roles: Dict[str, Role],
        all_tool_names: List[str],
    ):
        self._registry = registry
        self._roles = roles
        self._all_tools = all_tool_names

    def audit(self) -> dict:
        unregistered = [t for t in self._all_tools if t not in self._registry._descriptors]
        guest_role = self._roles.get("guest")
        guest_tools = self._registry.allowed_tools(guest_role) if guest_role else []

        role_coverage = {
            role_name: len(self._registry.allowed_tools(role))
            for role_name, role in self._roles.items()
        }

        return {
            "total_tools": len(self._all_tools),
            "rbac_registered": len(self._registry._descriptors),
            "unregistered_tools": unregistered,
            "guest_accessible_tools": guest_tools,
            "role_tool_counts": role_coverage,
        }
```

## Comparison

| Approach | Permission Model | Role Definition | Session Binding | Dispatch Enforcement | Coverage Audit |
|---|---|---|---|---|---|
| ToolPermissionDescriptor | Yes | No | No | No | No |
| RBACToolRegistry | Via descriptor | No | No | No | No |
| SessionRoleManager | No | No | Yes (TTL) | No | No |
| RBACToolDispatcher | Via registry | Via roles dict | Via manager | Yes | No |
| RBACCoverageAuditor | Via registry | Via roles dict | No | No | Yes |

**Best for production**: Filter the LLM's tool definitions by role at prompt construction time — a guest-role session should not even see admin tools in the system prompt, preventing the LLM from attempting to call them. Enforce again at dispatch time as a defense-in-depth layer. Use `expires_in_seconds` on role bindings for privilege escalation flows (e.g., operator access after MFA) so elevated roles are time-bounded. Run `RBACCoverageAuditor.audit()` as a CI gate to prevent new tools from being deployed without RBAC registration.
