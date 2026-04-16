---
title: "Agent Doesn't Implement User Identity Binding for Tool Authorizations"
description: "Agents that execute tool calls without binding them to a verified user identity allow privilege escalation through prompt injection: an injected instruction can cause the agent to invoke privileged tools on behalf of the current session even if the authenticated user has no permission to use those tools. Implement user identity binding that attaches a verified identity token to every tool call, enforces per-tool permission checks against the bound identity, and rejects tool calls that exceed the user's authorization scope."
date: 2026-04-16
difficulty: advanced
category: security
slug: agent-doesnt-implement-user-identity-binding-for-tool-authorizations
tags: [identity-binding, authorization, tool-permissions, privilege-escalation, rbac, prompt-injection]
symptoms:
  - "Agent executes admin-level tool calls when session was authenticated with a low-privilege token"
  - "No per-tool authorization check — any authenticated session can invoke any tool"
  - "Prompt injection causes agent to call tools the user was never granted access to"
  - "Tool permissions are checked at registration time but not at invocation time"
  - "No audit trail linking tool call executions to the specific user who triggered them"
---

## Why This Happens

Agents decouple authentication (who is the user) from authorization (what tools can they use). Authentication typically happens at the API gateway or session layer and produces a token, but that token is rarely propagated into the tool dispatch layer. The agent executes tool calls based on LLM output without asking whether the authenticated user is allowed to invoke the requested tool. Prompt injection exploits this gap: an injected instruction can name a privileged tool that the LLM will happily call, because the dispatch layer never checks permissions. Binding the user identity to tool invocations and enforcing per-tool permission checks closes this gap.

## Solution 1: User Identity Token

```python
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional
import time


@dataclass
class UserIdentityToken:
    user_id: str
    session_id: str
    roles: FrozenSet[str]
    permissions: FrozenSet[str]     # explicit permission grants, e.g. "tool:file_read"
    issued_at: float = field(default_factory=time.time)
    expires_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def has_permission(self, permission: str) -> bool:
        return permission in self.permissions

    def has_role(self, role: str) -> bool:
        return role in self.roles
```

## Solution 2: Tool Permission Descriptor

```python
from dataclasses import dataclass, field
from enum import Enum
from typing import FrozenSet, List, Optional


class AuthzMode(str, Enum):
    ALLOW_ALL = "allow_all"           # no restriction — all authenticated users
    REQUIRE_PERMISSION = "require_permission"  # explicit permission grant needed
    REQUIRE_ROLE = "require_role"     # one of the listed roles needed
    DENY_ALL = "deny_all"             # tool disabled for all users


@dataclass
class ToolPermissionDescriptor:
    tool_name: str
    authz_mode: AuthzMode = AuthzMode.ALLOW_ALL
    required_permissions: FrozenSet[str] = field(default_factory=frozenset)
    required_roles: FrozenSet[str] = field(default_factory=frozenset)
    # At least one role or one permission must match (OR logic within each set)
    risk_level: str = "low"           # "low" | "medium" | "high" | "critical"
    audit_all_invocations: bool = False

    def is_authorized(self, identity: UserIdentityToken) -> tuple[bool, str]:
        if self.authz_mode == AuthzMode.DENY_ALL:
            return False, f"Tool '{self.tool_name}' is disabled for all users"

        if self.authz_mode == AuthzMode.ALLOW_ALL:
            return True, ""

        if self.authz_mode == AuthzMode.REQUIRE_PERMISSION:
            for perm in self.required_permissions:
                if identity.has_permission(perm):
                    return True, ""
            return False, (
                f"User '{identity.user_id}' lacks required permission for tool "
                f"'{self.tool_name}'. Required: {self.required_permissions}"
            )

        if self.authz_mode == AuthzMode.REQUIRE_ROLE:
            for role in self.required_roles:
                if identity.has_role(role):
                    return True, ""
            return False, (
                f"User '{identity.user_id}' lacks required role for tool "
                f"'{self.tool_name}'. Required: {self.required_roles}"
            )

        return False, "Unknown authorization mode"
```

## Solution 3: Tool Authorization Registry

```python
from typing import Dict, List, Optional


class ToolAuthorizationRegistry:
    """
    Stores permission descriptors for all registered tools.
    Returns the descriptor for a tool or a default-deny descriptor
    for unregistered tools.
    """

    def __init__(self, default_mode: AuthzMode = AuthzMode.ALLOW_ALL):
        self._descriptors: Dict[str, ToolPermissionDescriptor] = {}
        self._default_mode = default_mode

    def register(self, descriptor: ToolPermissionDescriptor) -> None:
        self._descriptors[descriptor.tool_name] = descriptor

    def get(self, tool_name: str) -> ToolPermissionDescriptor:
        if tool_name in self._descriptors:
            return self._descriptors[tool_name]
        # Unregistered tools use default mode
        return ToolPermissionDescriptor(
            tool_name=tool_name,
            authz_mode=self._default_mode,
        )

    def list_by_risk(self, risk_level: str) -> List[ToolPermissionDescriptor]:
        return [d for d in self._descriptors.values() if d.risk_level == risk_level]
```

## Solution 4: Identity-Bound Tool Dispatcher

```python
import asyncio
import time
from typing import Any, Callable, Dict, Optional


class IdentityBoundToolDispatcher:
    """
    Wraps tool execution with identity verification and authorization checks.
    Every tool call is bound to the current session's user identity token.
    """

    def __init__(
        self,
        authz_registry: ToolAuthorizationRegistry,
        audit_logger: Optional["ToolAuthorizationAuditLogger"] = None,
    ):
        self._registry = authz_registry
        self._audit = audit_logger
        self._total_calls = 0
        self._denied_calls = 0

    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        tool_fn: Callable,
        identity: UserIdentityToken,
    ) -> Any:
        self._total_calls += 1

        # Identity expiry check
        if identity.is_expired():
            self._denied_calls += 1
            raise ToolAuthorizationError(
                tool_name=tool_name,
                user_id=identity.user_id,
                reason="Identity token has expired",
            )

        descriptor = self._registry.get(tool_name)
        authorized, reason = descriptor.is_authorized(identity)

        if self._audit and (not authorized or descriptor.audit_all_invocations):
            self._audit.record(
                tool_name=tool_name,
                user_id=identity.user_id,
                session_id=identity.session_id,
                authorized=authorized,
                reason=reason,
                risk_level=descriptor.risk_level,
            )

        if not authorized:
            self._denied_calls += 1
            raise ToolAuthorizationError(
                tool_name=tool_name,
                user_id=identity.user_id,
                reason=reason,
            )

        return await tool_fn(**args)

    def stats(self) -> dict:
        return {
            "total_calls": self._total_calls,
            "denied_calls": self._denied_calls,
            "deny_rate_pct": round(
                self._denied_calls / max(self._total_calls, 1) * 100, 2
            ),
        }


class ToolAuthorizationError(Exception):
    def __init__(self, tool_name: str, user_id: str, reason: str):
        super().__init__(
            f"Authorization denied for tool '{tool_name}' by user '{user_id}': {reason}"
        )
        self.tool_name = tool_name
        self.user_id = user_id
        self.reason = reason
```

## Solution 5: Tool Authorization Audit Logger

```python
import time
from typing import List


class ToolAuthorizationAuditLogger:
    """
    Records authorization decisions for tool calls — both grants and denials.
    Surfaces privilege escalation attempts and patterns of unauthorized access.
    """

    def __init__(self, max_records: int = 50000):
        self._max = max_records
        self._records: List[dict] = []

    def record(
        self,
        tool_name: str,
        user_id: str,
        session_id: str,
        authorized: bool,
        reason: str,
        risk_level: str = "low",
    ) -> None:
        if len(self._records) >= self._max:
            self._records.pop(0)
        self._records.append({
            "ts": time.time(),
            "tool_name": tool_name,
            "user_id": user_id,
            "session_id": session_id,
            "authorized": authorized,
            "reason": reason if not authorized else "",
            "risk_level": risk_level,
        })

    def summary(self, window_seconds: float = 3600.0) -> dict:
        cutoff = time.time() - window_seconds
        recent = [r for r in self._records if r["ts"] >= cutoff]
        denials = [r for r in recent if not r["authorized"]]
        high_risk_denials = [r for r in denials if r["risk_level"] in ("high", "critical")]
        return {
            "window_seconds": window_seconds,
            "total_calls": len(recent),
            "denied_calls": len(denials),
            "high_risk_denials": len(high_risk_denials),
            "unique_denied_users": len({r["user_id"] for r in denials}),
        }
```

## Solution 6: Authorization Dashboard

```python
import time


class ToolAuthorizationDashboard:
    """
    Combines dispatcher statistics, audit summaries, and high-risk
    tool exposure into a single security and compliance view.
    """

    def __init__(
        self,
        dispatcher: IdentityBoundToolDispatcher,
        registry: ToolAuthorizationRegistry,
        audit_logger: ToolAuthorizationAuditLogger,
    ):
        self._dispatcher = dispatcher
        self._registry = registry
        self._audit = audit_logger

    def render(self) -> dict:
        critical_tools = [
            d.tool_name for d in self._registry.list_by_risk("critical")
        ]
        high_tools = [
            d.tool_name for d in self._registry.list_by_risk("high")
        ]
        return {
            "generated_at": time.time(),
            "dispatcher_stats": self._dispatcher.stats(),
            "high_risk_tools": high_tools,
            "critical_risk_tools": critical_tools,
            "audit_1h": self._audit.summary(3600.0),
            "audit_24h": self._audit.summary(86400.0),
        }
```

## Comparison

| Approach | Identity Binding | Per-Tool Permissions | RBAC | Audit Trail | Dashboard |
|---|---|---|---|---|---|
| UserIdentityToken | Yes | No | Yes (roles) | No | No |
| ToolPermissionDescriptor | No | Yes (authz modes) | Yes | No | No |
| ToolAuthorizationRegistry | No | Via descriptors | Via descriptors | No | No |
| IdentityBoundToolDispatcher | Yes (per call) | Via registry | Via registry | Via logger | No |
| ToolAuthorizationAuditLogger | No | No | No | Yes | No |
| ToolAuthorizationDashboard | No | No | No | No | Yes |

**Best for production**: Default the registry to `AuthzMode.DENY_ALL` for unregistered tools — this ensures new tools must be explicitly granted permissions before the agent can invoke them, rather than inheriting an implicit allow. Mark payment, data-export, admin, and account-mutation tools as `risk_level="critical"` and set `audit_all_invocations=True`: every invocation of these tools — successful or not — should appear in the audit log for compliance. Propagate the `UserIdentityToken` from the session layer via a request context object rather than passing it as an explicit parameter to every tool call — this prevents it from appearing in LLM-visible tool arguments where it could be manipulated by prompt injection. Rotate identity tokens with short expiry (15-30 minutes) and refresh them through the session layer rather than the agent: expired token detection in `IdentityBoundToolDispatcher` then catches sessions that were hijacked after the original authentication.
