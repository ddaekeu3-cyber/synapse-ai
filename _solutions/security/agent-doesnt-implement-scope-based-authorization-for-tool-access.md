---
title: "Agent Doesn't Implement Scope-Based Authorization for Tool Access"
description: "AI agents that allow any authenticated caller to invoke any tool ignore the principle of least privilege. Scope-based authorization assigns fine-grained permission strings to each tool and validates caller scopes before execution — a read-only API key can call search tools but not write tools, and a user-tier token cannot invoke admin tools regardless of authentication."
date: 2025-02-17
difficulty: intermediate
category: security
slug: agent-doesnt-implement-scope-based-authorization-for-tool-access
tags:
  - authorization
  - scope
  - least-privilege
  - tool-access
  - security
  - oauth
  - permissions
symptoms:
  - "A read-only API key successfully calls a delete_record tool"
  - "User-tier tokens can invoke admin_tools that should be restricted to operators"
  - "All tools are accessible to any authenticated caller regardless of their permission level"
  - "No audit trail of which caller invoked which tool"
  - "A compromised low-privilege token can exfiltrate data via unrestricted tool access"
---

## Problem

Authentication (proving identity) and authorization (proving permission) are separate concerns. An agent that only checks authentication allows any valid credential to call any tool. Scope-based authorization assigns each tool one or more required scopes, validates that the caller's token contains those scopes before execution, and rejects the call with a 403 if any required scope is missing. This limits blast radius: a compromised read-only token cannot trigger destructive tools, and a user-tier credential cannot access admin endpoints.

---

## Solution 1: ToolScopeRegistry — Declare Required Scopes per Tool

```python
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set


@dataclass
class ToolScopeSpec:
    tool_name: str
    required_scopes: FrozenSet[str]    # ALL must be present (AND)
    any_of_scopes: FrozenSet[str]      # At least ONE must be present (OR)
    description: str = ""

    def is_satisfied_by(self, caller_scopes: Set[str]) -> bool:
        if self.required_scopes and not self.required_scopes.issubset(caller_scopes):
            return False
        if self.any_of_scopes and not self.any_of_scopes.intersection(caller_scopes):
            return False
        return True

    def missing_scopes(self, caller_scopes: Set[str]) -> List[str]:
        missing = list(self.required_scopes - caller_scopes)
        if self.any_of_scopes and not self.any_of_scopes.intersection(caller_scopes):
            missing.append(f"one_of:{','.join(sorted(self.any_of_scopes))}")
        return missing


class ToolScopeRegistry:
    """
    Declares the scope requirements for each registered tool.
    Tools without explicit registration default to requiring authentication
    only (no extra scopes), or to the deny-by-default policy if configured.

    Usage:
        registry = ToolScopeRegistry(deny_unregistered=True)
        registry.register("web_search",
                           required={"tools:read"},
                           description="Public search — read scope only")
        registry.register("delete_record",
                           required={"tools:write", "data:delete"},
                           description="Destructive — needs write AND delete")
        registry.register("admin_config",
                           any_of={"role:admin", "role:operator"},
                           description="Config changes — admin or operator role")
    """

    def __init__(self, deny_unregistered: bool = False):
        self._specs: Dict[str, ToolScopeSpec] = {}
        self._deny_unregistered = deny_unregistered

    def register(self, tool_name: str,
                  required: Optional[Set[str]] = None,
                  any_of: Optional[Set[str]] = None,
                  description: str = ""):
        self._specs[tool_name] = ToolScopeSpec(
            tool_name=tool_name,
            required_scopes=frozenset(required or set()),
            any_of_scopes=frozenset(any_of or set()),
            description=description,
        )

    def spec_for(self, tool_name: str) -> Optional[ToolScopeSpec]:
        return self._specs.get(tool_name)

    def is_authorized(self, tool_name: str,
                       caller_scopes: Set[str]) -> bool:
        spec = self._specs.get(tool_name)
        if spec is None:
            return not self._deny_unregistered
        return spec.is_satisfied_by(caller_scopes)

    def missing_scopes(self, tool_name: str,
                        caller_scopes: Set[str]) -> List[str]:
        spec = self._specs.get(tool_name)
        if spec is None:
            return ["tool_not_registered"] if self._deny_unregistered else []
        return spec.missing_scopes(caller_scopes)

    def tools_accessible_to(self, caller_scopes: Set[str]) -> List[str]:
        return [
            name for name, spec in self._specs.items()
            if spec.is_satisfied_by(caller_scopes)
        ]
```

---

## Solution 2: CallerScopeExtractor — Parse Scopes from JWT or API Key

```python
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class CallerIdentity:
    caller_id: str
    scopes: Set[str]
    tier: str           # "user", "operator", "admin", "service"
    raw_claims: Dict[str, Any]

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_all(self, scopes: Set[str]) -> bool:
        return scopes.issubset(self.scopes)

    def has_any(self, scopes: Set[str]) -> bool:
        return bool(scopes.intersection(self.scopes))


class CallerScopeExtractor:
    """
    Extracts caller identity and scopes from a JWT token or API key.
    For JWTs: reads the 'scope' claim (space-separated string) and 'sub'.
    For API keys: looks up the key in a key->scopes mapping.

    Usage:
        extractor = CallerScopeExtractor(
            api_key_map={
                "sk-readonly-abc123": {
                    "caller_id": "service-A",
                    "scopes": {"tools:read"},
                    "tier": "service",
                }
            }
        )
        identity = extractor.from_bearer(auth_header)
        if identity is None:
            return 401, "Invalid credentials"
    """

    def __init__(self, api_key_map: Optional[Dict[str, Dict]] = None):
        self._key_map = api_key_map or {}

    def from_bearer(self, authorization: str) -> Optional[CallerIdentity]:
        if not authorization:
            return None
        parts = authorization.split(" ", 1)
        if len(parts) != 2:
            return None
        scheme, token = parts
        if scheme.lower() == "bearer":
            return self._from_jwt(token)
        if scheme.lower() == "apikey":
            return self._from_api_key(token)
        return None

    def _from_jwt(self, token: str) -> Optional[CallerIdentity]:
        """Decode JWT payload (no signature verification — caller must verify separately)."""
        try:
            parts = token.split(".")
            if len(parts) != 3:
                return None
            # Add padding
            payload_b64 = parts[1] + "=="
            payload_bytes = base64.urlsafe_b64decode(payload_b64)
            claims = json.loads(payload_bytes)

            raw_scope = claims.get("scope", "")
            scopes: Set[str] = set(raw_scope.split()) if raw_scope else set()

            # Also read roles as scopes
            roles = claims.get("roles", claims.get("role", []))
            if isinstance(roles, str):
                roles = [roles]
            for role in roles:
                scopes.add(f"role:{role}")

            tier = claims.get("tier", "user")
            scopes.add(f"tier:{tier}")

            return CallerIdentity(
                caller_id=claims.get("sub", "unknown"),
                scopes=scopes,
                tier=tier,
                raw_claims=claims,
            )
        except Exception as exc:
            logger.warning("jwt_decode_error: %s", exc)
            return None

    def _from_api_key(self, key: str) -> Optional[CallerIdentity]:
        entry = self._key_map.get(key)
        if entry is None:
            return None
        return CallerIdentity(
            caller_id=entry.get("caller_id", "unknown"),
            scopes=set(entry.get("scopes", [])),
            tier=entry.get("tier", "service"),
            raw_claims=entry,
        )
```

---

## Solution 3: ScopeEnforcingToolGateway — Enforce at Tool Invocation Time

```python
import logging
import time
from typing import Any, Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)


class AuthorizationError(Exception):
    def __init__(self, tool: str, missing: list, caller: str):
        self.tool = tool
        self.missing = missing
        self.caller = caller
        super().__init__(
            f"Caller '{caller}' lacks scopes {missing} to call '{tool}'"
        )


class ScopeEnforcingToolGateway:
    """
    Wraps tool calls with scope enforcement. Before executing any tool,
    validates that the caller's identity has the required scopes.
    Raises AuthorizationError (HTTP 403) for insufficient scopes.

    Usage:
        gateway = ScopeEnforcingToolGateway(scope_registry, audit_logger)
        gateway.register_tool("web_search", web_search_fn)
        gateway.register_tool("delete_record", delete_fn)

        identity = extractor.from_bearer(request.headers["Authorization"])
        result = await gateway.call("web_search", identity, query="AI safety")
    """

    def __init__(self, registry: ToolScopeRegistry,
                  audit_logger=None):
        self._registry = registry
        self._audit = audit_logger
        self._tools: Dict[str, Callable] = {}

    def register_tool(self, name: str, fn: Callable):
        self._tools[name] = fn

    async def call(self, tool_name: str,
                    identity: CallerIdentity,
                    *args, **kwargs) -> Any:
        missing = self._registry.missing_scopes(tool_name, identity.scopes)
        if missing:
            self._log_denied(tool_name, identity, missing)
            raise AuthorizationError(tool_name, missing, identity.caller_id)

        fn = self._tools.get(tool_name)
        if fn is None:
            raise KeyError(f"Tool '{tool_name}' not registered in gateway")

        t0 = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
            if self._audit:
                self._audit.record_allowed(tool_name, identity, time.monotonic() - t0)
            return result
        except Exception as exc:
            if self._audit:
                self._audit.record_error(tool_name, identity, exc)
            raise

    def _log_denied(self, tool: str, identity: CallerIdentity,
                     missing: list):
        logger.warning(
            "tool_access_denied tool=%s caller=%s tier=%s missing_scopes=%s",
            tool, identity.caller_id, identity.tier, missing,
        )
        if self._audit:
            self._audit.record_denied(tool, identity, missing)

    def accessible_tools(self, identity: CallerIdentity) -> list:
        return self._registry.tools_accessible_to(identity.scopes)
```

---

## Solution 4: ScopeAuditLogger — Record All Authorization Decisions

```python
import hashlib
import json
import logging
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class ScopeAuditLogger:
    """
    Records every tool authorization decision (allowed, denied, error)
    with caller identity hash, tool name, scopes, and outcome.
    Used for security forensics and compliance reporting.

    Usage:
        audit = ScopeAuditLogger()
        gateway = ScopeEnforcingToolGateway(registry, audit_logger=audit)

        # After agent run:
        denied = audit.denied_events()
        if denied:
            alert_security_team(denied)
    """

    def __init__(self, max_events: int = 10_000):
        self._events: List[Dict[str, Any]] = []
        self._max = max_events
        self._denied_count = 0
        self._allowed_count = 0

    def _caller_hash(self, identity: CallerIdentity) -> str:
        return hashlib.sha256(identity.caller_id.encode()).hexdigest()[:10]

    def record_allowed(self, tool: str, identity: CallerIdentity,
                        elapsed_s: float):
        self._allowed_count += 1
        self._append({
            "outcome": "allowed",
            "tool": tool,
            "caller": self._caller_hash(identity),
            "tier": identity.tier,
            "elapsed_ms": round(elapsed_s * 1000, 1),
            "ts": time.time(),
        })

    def record_denied(self, tool: str, identity: CallerIdentity,
                       missing: list):
        self._denied_count += 1
        logger.warning(
            "scope_audit_denied tool=%s caller=%s tier=%s missing=%s",
            tool, self._caller_hash(identity), identity.tier, missing,
        )
        self._append({
            "outcome": "denied",
            "tool": tool,
            "caller": self._caller_hash(identity),
            "tier": identity.tier,
            "missing_scopes": missing,
            "ts": time.time(),
        })

    def record_error(self, tool: str, identity: CallerIdentity,
                      exc: Exception):
        self._append({
            "outcome": "error",
            "tool": tool,
            "caller": self._caller_hash(identity),
            "error": type(exc).__name__,
            "ts": time.time(),
        })

    def _append(self, event: Dict[str, Any]):
        self._events.append(event)
        if len(self._events) > self._max:
            self._events = self._events[-self._max:]

    def denied_events(self, last_n: int = 100) -> List[Dict[str, Any]]:
        return [e for e in self._events[-last_n:] if e["outcome"] == "denied"]

    def summary(self) -> Dict[str, Any]:
        return {
            "allowed": self._allowed_count,
            "denied": self._denied_count,
            "denial_rate": round(
                self._denied_count / max(self._allowed_count + self._denied_count, 1),
                3,
            ),
        }
```

---

## Solution 5: DynamicScopeElevation — Temporary Scope Grants for Privileged Operations

```python
import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class ElevationGrant:
    grant_id: str
    caller_id: str
    elevated_scopes: Set[str]
    expires_at: float
    reason: str


class DynamicScopeElevation:
    """
    Grants temporary additional scopes to a caller for a bounded window.
    Used for sudo-style privilege escalation: an operator can approve
    a temporary elevation that allows a user-tier agent to call admin tools
    for a specific approved operation, then the elevation expires automatically.

    Usage:
        elevation = DynamicScopeElevation()
        grant_id = elevation.grant(
            caller_id="agent-U123",
            scopes={"tools:write"},
            duration_s=300,
            reason="approved by operator for migration task OP-42",
        )
        identity = elevation.elevate(base_identity, grant_id)
        # identity now has tools:write for 5 minutes
    """

    def __init__(self):
        self._grants: Dict[str, ElevationGrant] = {}

    def grant(self, caller_id: str,
               scopes: Set[str],
               duration_s: float = 300.0,
               reason: str = "") -> str:
        grant_id = str(uuid.uuid4())[:8]
        self._grants[grant_id] = ElevationGrant(
            grant_id=grant_id,
            caller_id=caller_id,
            elevated_scopes=frozenset(scopes),
            expires_at=time.monotonic() + duration_s,
            reason=reason,
        )
        logger.warning(
            "scope_elevation_granted caller=%s scopes=%s duration_s=%.0f reason=%s",
            caller_id, scopes, duration_s, reason,
        )
        return grant_id

    def elevate(self, identity: CallerIdentity,
                 grant_id: str) -> CallerIdentity:
        grant = self._grants.get(grant_id)
        if grant is None:
            return identity
        if grant.caller_id != identity.caller_id:
            logger.warning(
                "elevation_mismatch grant=%s expected=%s got=%s",
                grant_id, grant.caller_id, identity.caller_id,
            )
            return identity
        if time.monotonic() > grant.expires_at:
            logger.info("elevation_expired grant=%s", grant_id)
            self._grants.pop(grant_id, None)
            return identity

        elevated_scopes = identity.scopes | grant.elevated_scopes
        return CallerIdentity(
            caller_id=identity.caller_id,
            scopes=elevated_scopes,
            tier=identity.tier,
            raw_claims={**identity.raw_claims, "_elevated_by": grant_id},
        )

    def revoke(self, grant_id: str):
        self._grants.pop(grant_id, None)
        logger.info("scope_elevation_revoked grant=%s", grant_id)
```

---

## Solution 6: AuthorizedAgentPipeline — Full Scope Enforcement Stack

```python
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


class AuthorizedAgentPipeline:
    """
    Integrates scope registry, scope extraction, enforcing gateway,
    audit logging, and optional elevation into one pipeline object.

    Usage:
        pipeline = AuthorizedAgentPipeline(deny_unregistered=True)
        pipeline.declare_tool("web_search",   web_fn,    required={"tools:read"})
        pipeline.declare_tool("delete_record", delete_fn, required={"tools:write", "data:delete"})
        pipeline.declare_tool("admin_config",  admin_fn,  any_of={"role:admin"})

        identity = pipeline.authenticate(request.headers.get("Authorization"))
        if not identity:
            return 401

        result = await pipeline.call("web_search", identity, query="climate")
    """

    def __init__(self,
                  deny_unregistered: bool = True,
                  api_key_map: Optional[Dict] = None):
        self._registry = ToolScopeRegistry(deny_unregistered=deny_unregistered)
        self._extractor = CallerScopeExtractor(api_key_map=api_key_map)
        self._audit = ScopeAuditLogger()
        self._gateway = ScopeEnforcingToolGateway(
            self._registry, audit_logger=self._audit
        )
        self._elevation = DynamicScopeElevation()

    def declare_tool(self, name: str, fn: Callable,
                      required: Optional[set] = None,
                      any_of: Optional[set] = None,
                      description: str = ""):
        self._registry.register(name, required=required,
                                  any_of=any_of, description=description)
        self._gateway.register_tool(name, fn)

    def authenticate(self, authorization: Optional[str]) -> Optional[CallerIdentity]:
        if not authorization:
            return None
        return self._extractor.from_bearer(authorization)

    async def call(self, tool_name: str,
                    identity: CallerIdentity,
                    grant_id: Optional[str] = None,
                    **kwargs) -> Any:
        if grant_id:
            identity = self._elevation.elevate(identity, grant_id)
        return await self._gateway.call(tool_name, identity, **kwargs)

    def grant_elevation(self, caller_id: str, scopes: set,
                         duration_s: float = 300.0,
                         reason: str = "") -> str:
        return self._elevation.grant(caller_id, scopes, duration_s, reason)

    def accessible_tools(self, identity: CallerIdentity) -> list:
        return self._gateway.accessible_tools(identity)

    def audit_summary(self) -> Dict[str, Any]:
        return self._audit.summary()
```

---

## Comparison

| Approach | Scope Declaration | Scope Extraction | Enforcement | Audit | Dynamic Elevation | Integrated |
|---|---|---|---|---|---|---|
| **ToolScopeRegistry** | Yes | No | No | No | No | No |
| **CallerScopeExtractor** | No | Yes | No | No | No | No |
| **ScopeEnforcingToolGateway** | No | No | Yes | No | No | No |
| **ScopeAuditLogger** | No | No | No | Yes | No | No |
| **DynamicScopeElevation** | No | No | No | No | Yes | No |
| **AuthorizedAgentPipeline** | Yes | Yes | Yes | Yes | Yes | Yes |

**Key insight**: design scopes as fine-grained permission strings rather than coarse roles (`tools:read` rather than `user`). Separate read and write scopes for every data domain so a leaked read-only key cannot trigger mutations. Use the `any_of` pattern for role-based access (`role:admin` OR `role:operator`) and `required` for capability-based access (`tools:write` AND `data:delete`). Always log denied calls — a spike in 403 events for a specific tool indicates either a misconfigured caller or an active privilege-escalation attempt, and the audit log is the only evidence.
