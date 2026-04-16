---
title: "Agent Doesn't Implement Tool Permission Scoping by Conversation Role"
description: "Agents that grant every caller access to every tool allow a low-privilege user to invoke administrative tools — database schema modifications, user management endpoints, infrastructure commands — that should only be accessible to authenticated operators. Implement tool permission scoping that restricts tool availability based on the caller's conversation role, preventing privilege escalation through the agent interface."
date: 2026-04-16
difficulty: intermediate
category: security
slug: agent-doesnt-implement-tool-permission-scoping-by-conversation-role
tags: [tool-permissions, rbac, conversation-role, privilege-escalation, access-control, tool-authorization]
symptoms:
  - "All tools are available to all callers regardless of authentication level"
  - "An unauthenticated user can invoke database write tools through the agent"
  - "No distinction between read-only tools and mutating or administrative tools"
  - "Operator-only tools like 'delete_user' are visible in the LLM context for every session"
  - "Privilege escalation: a user can ask the agent to call tools they would never be granted directly"
---

## Why This Happens

Agents build their tool list at startup and inject it into the LLM context unchanged for every call. When a user asks the agent to "delete all test records," the agent obligingly calls `delete_records()` because the tool is in scope. Tool permission scoping requires associating each tool with one or more required roles, extracting the caller's role from the session context, and filtering the tool list to only tools the caller is authorized to invoke before building the LLM context.

## Solution 1: Tool Permission Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Set


class ConversationRole(str, Enum):
    ANONYMOUS = "anonymous"
    USER = "user"
    PRO_USER = "pro_user"
    OPERATOR = "operator"
    ADMIN = "admin"
    INTERNAL = "internal"


ROLE_HIERARCHY = {
    ConversationRole.ANONYMOUS: 0,
    ConversationRole.USER: 1,
    ConversationRole.PRO_USER: 2,
    ConversationRole.OPERATOR: 3,
    ConversationRole.ADMIN: 4,
    ConversationRole.INTERNAL: 5,
}


@dataclass
class ToolPermissionDescriptor:
    tool_name: str
    required_roles: Set[ConversationRole] = field(default_factory=set)
    min_role: Optional[ConversationRole] = None   # all roles at this level or above
    deny_roles: Set[ConversationRole] = field(default_factory=set)
    audit_on_use: bool = False
    dangerous: bool = False

    def is_authorized(self, role: ConversationRole) -> bool:
        if role in self.deny_roles:
            return False
        if self.min_role is not None:
            return ROLE_HIERARCHY.get(role, 0) >= ROLE_HIERARCHY.get(self.min_role, 0)
        if self.required_roles:
            return role in self.required_roles
        return True   # no restrictions
```

## Solution 2: Tool Permission Registry

```python
from typing import Dict, List, Optional


class ToolPermissionRegistry:
    """
    Maps tool names to their permission descriptors.
    Provides role-scoped tool lists for LLM context injection.
    """

    def __init__(self):
        self._descriptors: Dict[str, ToolPermissionDescriptor] = {}

    def register(self, descriptor: ToolPermissionDescriptor) -> None:
        self._descriptors[descriptor.tool_name] = descriptor

    def register_many(self, descriptors: List[ToolPermissionDescriptor]) -> None:
        for d in descriptors:
            self.register(d)

    def allowed_tools(self, role: ConversationRole) -> List[str]:
        return [
            name for name, desc in self._descriptors.items()
            if desc.is_authorized(role)
        ]

    def is_allowed(self, tool_name: str, role: ConversationRole) -> bool:
        desc = self._descriptors.get(tool_name)
        if desc is None:
            return False   # unknown tools are denied by default
        return desc.is_authorized(role)

    def requires_audit(self, tool_name: str) -> bool:
        desc = self._descriptors.get(tool_name)
        return desc.audit_on_use if desc else False

    def dangerous_tools(self) -> List[str]:
        return [name for name, d in self._descriptors.items() if d.dangerous]
```

## Solution 3: Role-Scoped Tool Context Builder

```python
from typing import Any, Dict, List, Optional


class RoleScopedToolContextBuilder:
    """
    Builds the tool list for LLM context injection filtered
    to only those tools the current caller's role can invoke.
    Dangerous tools are additionally annotated.
    """

    def __init__(
        self,
        registry: ToolPermissionRegistry,
        all_tool_schemas: Dict[str, Dict[str, Any]],
    ):
        self._registry = registry
        self._schemas = all_tool_schemas

    def build_tool_list(
        self,
        role: ConversationRole,
        include_dangerous: bool = False,
    ) -> List[Dict[str, Any]]:
        allowed = self._registry.allowed_tools(role)
        result = []
        for name in allowed:
            schema = self._schemas.get(name)
            if schema is None:
                continue
            if self._registry._descriptors[name].dangerous and not include_dangerous:
                continue
            result.append(schema)
        return result

    def tool_count_by_role(self) -> Dict[str, int]:
        return {
            role.value: len(self._registry.allowed_tools(role))
            for role in ConversationRole
        }
```

## Solution 4: Permission-Enforcing Tool Dispatcher

```python
from typing import Any, Callable, Dict


class PermissionEnforcingToolDispatcher:
    """
    Intercepts tool calls and verifies the caller's role before dispatch.
    Raises ToolPermissionDeniedError for unauthorized calls.
    Logs audit events for tools marked audit_on_use.
    """

    def __init__(
        self,
        registry: ToolPermissionRegistry,
        audit_logger: Optional[Any] = None,
    ):
        self._registry = registry
        self._audit = audit_logger
        self._denied_count = 0
        self._allowed_count = 0

    async def dispatch(
        self,
        tool_name: str,
        role: ConversationRole,
        session_id: str,
        fn: Callable,
        **kwargs: Any,
    ) -> Any:
        if not self._registry.is_allowed(tool_name, role):
            self._denied_count += 1
            if self._audit:
                self._audit.record_denial(tool_name, role, session_id)
            raise ToolPermissionDeniedError(tool_name, role)

        self._allowed_count += 1
        if self._registry.requires_audit(tool_name) and self._audit:
            self._audit.record_use(tool_name, role, session_id, kwargs)

        return await fn(**kwargs)

    def stats(self) -> dict:
        total = self._allowed_count + self._denied_count
        return {
            "allowed": self._allowed_count,
            "denied": self._denied_count,
            "denial_rate": round(self._denied_count / max(total, 1), 4),
        }


class ToolPermissionDeniedError(Exception):
    def __init__(self, tool_name: str, role: ConversationRole):
        super().__init__(
            f"tool '{tool_name}' not authorized for role '{role.value}'"
        )
        self.tool_name = tool_name
        self.role = role
```

## Solution 5: Permission Denial Audit Logger

```python
import time
from typing import Any, Dict, List, Optional


class ToolPermissionAuditLogger:
    """
    Records permission denials and sensitive tool uses for security analysis.
    """

    def __init__(self, max_records: int = 10000):
        self._records: List[dict] = []
        self._max = max_records

    def record_denial(
        self,
        tool_name: str,
        role: ConversationRole,
        session_id: str,
    ) -> None:
        self._append({
            "event": "denial",
            "tool_name": tool_name,
            "role": role.value,
            "session_id": session_id,
        })

    def record_use(
        self,
        tool_name: str,
        role: ConversationRole,
        session_id: str,
        args: Dict[str, Any],
    ) -> None:
        self._append({
            "event": "audit_use",
            "tool_name": tool_name,
            "role": role.value,
            "session_id": session_id,
            "args_keys": list(args.keys()),
        })

    def _append(self, record: dict) -> None:
        record["ts"] = time.time()
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append(record)

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        denials = [r for r in recent if r["event"] == "denial"]
        tool_counts: dict = {}
        for r in denials:
            t = r["tool_name"]
            tool_counts[t] = tool_counts.get(t, 0) + 1
        return {
            "window_seconds": window_seconds,
            "total_denials": len(denials),
            "audit_uses": sum(1 for r in recent if r["event"] == "audit_use"),
            "top_denied_tools": sorted(tool_counts.items(), key=lambda x: -x[1])[:5],
        }
```

## Solution 6: Permission Scoping Dashboard

```python
import time


class ToolPermissionScopingDashboard:
    """
    Combines tool availability by role, denial stats, and audit summary.
    """

    def __init__(
        self,
        registry: ToolPermissionRegistry,
        builder: RoleScopedToolContextBuilder,
        dispatcher: PermissionEnforcingToolDispatcher,
        audit_logger: ToolPermissionAuditLogger,
    ):
        self._registry = registry
        self._builder = builder
        self._dispatcher = dispatcher
        self._audit = audit_logger

    def render(self) -> dict:
        return {
            "generated_at": time.time(),
            "tool_count_by_role": self._builder.tool_count_by_role(),
            "dangerous_tools": self._registry.dangerous_tools(),
            "dispatcher_stats": self._dispatcher.stats(),
            "audit_summary": self._audit.summary(window_seconds=3600.0),
        }
```

## Comparison

| Approach | Role-Based Filtering | Min-Role Hierarchy | Dispatch Enforcement | Audit Logging | Dashboard |
|---|---|---|---|---|---|
| ToolPermissionDescriptor | Yes | Yes (hierarchy) | No | No | No |
| ToolPermissionRegistry | Yes | Via descriptors | No | No | No |
| RoleScopedToolContextBuilder | Via registry | Via registry | No | No | No |
| PermissionEnforcingToolDispatcher | Via registry | Via registry | Yes | Via logger | No |
| ToolPermissionAuditLogger | No | No | No | Yes | No |

**Best for production**: Use `min_role` rather than explicit `required_roles` sets for most tools — the hierarchy means adding a new role between OPERATOR and ADMIN automatically inherits permissions without updating every descriptor. Mark all mutating tools (write, delete, update) as `dangerous=True` and `audit_on_use=True` regardless of role — an audit trail for every mutation is cheap and invaluable during incident investigation. Never inject the full tool list into the LLM context for low-privilege callers — the LLM can be prompted to call tools it sees even if the dispatcher would reject them; keeping dangerous tools out of the context is defense-in-depth.
