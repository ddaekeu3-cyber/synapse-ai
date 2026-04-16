---
title: "Agent doesn't implement tool call permission matrix"
description: "Every user can invoke every tool regardless of their role or trust level. A free-tier user can trigger expensive batch operations, an unauthenticated caller can write to the database, and a read-only session can execute shell commands."
difficulty: intermediate
category: security
tags: [authorization, RBAC, permissions, tool-access, least-privilege, access-control]
---

## Problem

Agents expose tools as capabilities, but without an explicit permission matrix, access control is absent. The agent's tool-use loop decides which tool to call based on the model's output — not based on whether the calling user is authorized to use that tool. This means:

- Free-tier users can invoke `batch_process` that costs $50/run
- Read-only API keys can execute `delete_record` or `run_shell_command`
- Unauthenticated webhook callers can access `list_all_users`
- Guest sessions can trigger `send_email` or `post_to_slack`

```python
# BAD: model decides which tool to call with no authorization check
async def agentic_loop(user_message, tools):
    response = await client.messages.create(tools=tools, ...)
    for tool_use in response.tool_uses:
        result = await execute_tool(tool_use.name, tool_use.input)
        # No check: is this user allowed to call tool_use.name?
```

## Solution 1: Role-based permission matrix with decorator-style enforcement

Define a matrix of `{role: [allowed_tools]}` and enforce it before every tool execution.

```python
from dataclasses import dataclass, field
from typing import Any
import functools


# ── Permission matrix ─────────────────────────────────────────────────
TOOL_PERMISSIONS: dict[str, set[str]] = {
    "guest": {
        "web_search", "get_weather", "get_public_info",
    },
    "user": {
        "web_search", "get_weather", "get_public_info",
        "read_document", "list_my_records", "send_message_to_self",
    },
    "pro_user": {
        "web_search", "get_weather", "get_public_info",
        "read_document", "list_my_records", "send_message_to_self",
        "create_document", "update_my_record", "run_analysis",
        "batch_embed",
    },
    "admin": {
        # Admins get everything
        "*",
    },
    "service_account": {
        "read_document", "list_all_records", "create_document",
        "update_any_record", "run_analysis", "batch_embed",
        "webhook_deliver",
    },
}


@dataclass
class UserContext:
    user_id: str
    role: str
    session_id: str
    metadata: dict = field(default_factory=dict)


class PermissionDeniedError(PermissionError):
    def __init__(self, user_id: str, role: str, tool: str):
        self.user_id = user_id
        self.role = role
        self.tool = tool
        super().__init__(
            f"User {user_id!r} (role={role!r}) is not authorized to call tool {tool!r}"
        )


def check_tool_permission(user: UserContext, tool_name: str) -> bool:
    allowed = TOOL_PERMISSIONS.get(user.role, set())
    if "*" in allowed:
        return True
    return tool_name in allowed


def require_permission(fn):
    """Decorator: enforce tool permission before execution."""
    @functools.wraps(fn)
    async def wrapper(tool_name: str, tool_input: dict, user: UserContext, **kwargs):
        if not check_tool_permission(user, tool_name):
            raise PermissionDeniedError(user.user_id, user.role, tool_name)
        return await fn(tool_name, tool_input, user, **kwargs)
    return wrapper


# ── Tool executor with permission enforcement ─────────────────────────
TOOL_REGISTRY: dict[str, Any] = {
    "web_search": lambda args: {"results": [f"result for {args.get('query')}"]},
    "delete_record": lambda args: {"deleted": args.get("record_id")},
    "run_shell_command": lambda args: {"output": "$ " + args.get("command", "")},
    "batch_embed": lambda args: {"embeddings": []},
}


@require_permission
async def execute_tool(tool_name: str, tool_input: dict, user: UserContext) -> dict:
    import asyncio
    handler = TOOL_REGISTRY.get(tool_name)
    if handler is None:
        raise ValueError(f"Unknown tool: {tool_name}")
    await asyncio.sleep(0.001)
    return handler(tool_input)


# ── Demo ──────────────────────────────────────────────────────────────
import asyncio


async def demo():
    guest = UserContext(user_id="anon", role="guest", session_id="s1")
    admin = UserContext(user_id="alice", role="admin", session_id="s2")

    # Guest can search
    result = await execute_tool("web_search", {"query": "AI"}, user=guest)
    print(f"Guest web_search: {result}")

    # Guest cannot delete
    try:
        await execute_tool("delete_record", {"record_id": "42"}, user=guest)
    except PermissionDeniedError as e:
        print(f"DENIED: {e}")

    # Admin can do everything
    result = await execute_tool("run_shell_command", {"command": "ls"}, user=admin)
    print(f"Admin shell: {result}")


asyncio.run(demo())
```

## Solution 2: Attribute-based access control (ABAC) with dynamic rules

Instead of static role lists, evaluate rules against user attributes, tool metadata, and request context. More flexible — allows "pro users can embed up to 1000 texts/day" type policies.

```python
import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ToolMetadata:
    name: str
    sensitivity: str      # "public" | "internal" | "privileged" | "destructive"
    cost_tier: str        # "free" | "standard" | "expensive"
    rate_limit_key: str   # field name for per-user rate limiting
    requires_attrs: list[str] = field(default_factory=list)  # required user attrs


TOOL_METADATA: dict[str, ToolMetadata] = {
    "web_search":       ToolMetadata("web_search", "public", "free", "search_calls"),
    "read_document":    ToolMetadata("read_document", "internal", "free", "read_calls"),
    "batch_embed":      ToolMetadata("batch_embed", "internal", "expensive", "embed_calls"),
    "delete_record":    ToolMetadata("delete_record", "destructive", "standard", "write_calls"),
    "run_shell_command":ToolMetadata("run_shell_command", "privileged", "standard", "shell_calls"),
    "list_all_users":   ToolMetadata("list_all_users", "privileged", "standard", "admin_calls"),
}


@dataclass
class UserAttributes:
    user_id: str
    subscription: str    # "free" | "pro" | "enterprise"
    roles: list[str]
    verified: bool
    metadata: dict = field(default_factory=dict)


Rule = Callable[[UserAttributes, ToolMetadata, dict], bool]

ABAC_RULES: list[tuple[str, Rule]] = [
    (
        "block_unverified_from_internal",
        lambda u, t, _: not (t.sensitivity in ("internal", "privileged", "destructive") and not u.verified),
    ),
    (
        "block_free_tier_from_expensive",
        lambda u, t, _: not (t.cost_tier == "expensive" and u.subscription == "free"),
    ),
    (
        "block_non_admin_from_privileged",
        lambda u, t, _: not (t.sensitivity in ("privileged", "destructive") and "admin" not in u.roles),
    ),
]


def evaluate_abac(user: UserAttributes, tool_name: str, context: dict) -> tuple[bool, str]:
    meta = TOOL_METADATA.get(tool_name)
    if meta is None:
        return False, f"Unknown tool: {tool_name}"

    for rule_name, rule_fn in ABAC_RULES:
        if not rule_fn(user, meta, context):
            return False, f"Policy denied by rule: {rule_name}"

    return True, "allowed"


async def abac_execute(tool_name: str, tool_input: dict, user: UserAttributes) -> dict:
    allowed, reason = evaluate_abac(user, tool_name, {"input": tool_input})
    if not allowed:
        raise PermissionError(f"[{user.user_id}] {reason} for tool '{tool_name}'")
    return {"tool": tool_name, "result": "ok"}


# ── Demo ──────────────────────────────────────────────────────────────
async def demo():
    free_user = UserAttributes("bob", subscription="free", roles=["user"], verified=True)
    pro_user = UserAttributes("alice", subscription="pro", roles=["user"], verified=True)

    # Free user cannot use expensive tools
    try:
        await abac_execute("batch_embed", {"texts": ["a", "b"]}, free_user)
    except PermissionError as e:
        print(f"DENIED: {e}")

    # Pro user can
    result = await abac_execute("batch_embed", {"texts": ["a", "b"]}, pro_user)
    print(f"ALLOWED: {result}")


asyncio.run(demo())
```

## Solution 3: Tool permission matrix enforced in the agentic loop

Integrate permission checks directly into the model's tool-use response loop. Any tool call the model attempts that is not permitted is rejected and a denial message is fed back to the model.

```python
import asyncio
import json
from anthropic import AsyncAnthropic
from typing import Any

client = AsyncAnthropic()


class PermissionEnforcedAgentLoop:
    def __init__(self, tool_permissions: dict[str, set[str]]):
        self.tool_permissions = tool_permissions

    def _allowed_tools(self, role: str) -> set[str]:
        allowed = self.tool_permissions.get(role, set())
        if "*" in allowed:
            return {"*"}
        return allowed

    def _is_permitted(self, role: str, tool_name: str) -> bool:
        allowed = self._allowed_tools(role)
        return "*" in allowed or tool_name in allowed

    async def run(
        self,
        user_message: str,
        user_role: str,
        all_tools: list[dict],
        tool_executor: Any,
        max_turns: int = 10,
    ) -> str:
        messages = [{"role": "user", "content": user_message}]

        for _ in range(max_turns):
            response = await client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                tools=all_tools,
                messages=messages,
            )

            if response.stop_reason == "end_turn":
                return response.content[0].text if response.content else ""

            if response.stop_reason != "tool_use":
                break

            # Process tool use blocks
            tool_results = []
            messages.append({"role": "assistant", "content": response.content})

            for block in response.content:
                if block.type != "tool_use":
                    continue

                if self._is_permitted(user_role, block.name):
                    try:
                        result = await tool_executor(block.name, block.input)
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result),
                        })
                    except Exception as e:
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error: {e}",
                            "is_error": True,
                        })
                else:
                    # Inject permission denial back to model
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": (
                            f"Permission denied: role '{user_role}' is not authorized "
                            f"to call tool '{block.name}'. "
                            f"Please complete the task using only permitted tools."
                        ),
                        "is_error": True,
                    })

            messages.append({"role": "user", "content": tool_results})

        return "Agent loop completed"
```

## Solution 4: Scoped tool tokens — generate per-session capability tokens

Instead of checking permissions on every call, issue a signed capability token at session start that lists exactly which tools the session may use. The token is verified before each tool execution.

```python
import asyncio
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from typing import Optional


TOKEN_SECRET = os.environ.get("TOOL_TOKEN_SECRET", "change-me-in-production").encode()


@dataclass
class CapabilityToken:
    user_id: str
    session_id: str
    allowed_tools: list[str]
    issued_at: float
    expires_at: float
    signature: str = ""

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "allowed_tools": sorted(self.allowed_tools),
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
        }

    def is_expired(self) -> bool:
        return time.time() > self.expires_at

    def allows(self, tool: str) -> bool:
        return tool in self.allowed_tools or "*" in self.allowed_tools


def sign_token(token: CapabilityToken) -> str:
    payload = json.dumps(token.to_dict(), sort_keys=True).encode()
    return hmac.new(TOKEN_SECRET, payload, hashlib.sha256).hexdigest()


def issue_token(
    user_id: str,
    session_id: str,
    role: str,
    ttl_seconds: float = 3600.0,
) -> CapabilityToken:
    allowed = TOOL_PERMISSIONS.get(role, set())
    now = time.time()
    token = CapabilityToken(
        user_id=user_id,
        session_id=session_id,
        allowed_tools=list(allowed),
        issued_at=now,
        expires_at=now + ttl_seconds,
    )
    token.signature = sign_token(token)
    return token


def verify_token(token: CapabilityToken) -> bool:
    expected = sign_token(token)
    return hmac.compare_digest(expected, token.signature) and not token.is_expired()


TOOL_PERMISSIONS = {
    "user": {"web_search", "read_document"},
    "admin": {"*"},
}


async def token_guarded_execute(tool_name: str, tool_input: dict, token: CapabilityToken) -> dict:
    if not verify_token(token):
        raise PermissionError("Invalid or expired capability token")
    if not token.allows(tool_name):
        raise PermissionError(
            f"Capability token for {token.user_id!r} does not include tool {tool_name!r}"
        )
    return {"tool": tool_name, "result": "executed", "user": token.user_id}


# ── Demo ──────────────────────────────────────────────────────────────
async def demo():
    token = issue_token("alice", "sess-1", role="user", ttl_seconds=60)
    print(f"Token for alice: {token.allowed_tools}")

    result = await token_guarded_execute("web_search", {"query": "test"}, token)
    print(f"Allowed: {result}")

    try:
        await token_guarded_execute("delete_record", {"id": "1"}, token)
    except PermissionError as e:
        print(f"Denied: {e}")


asyncio.run(demo())
```

## Solution 5: Tool-level rate limiting per user role

Beyond allow/deny, enforce per-user, per-role rate limits on expensive tools. A pro user can call `batch_embed` 100 times/hour; a free user cannot call it at all.

```python
import asyncio
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RateLimit:
    calls_per_hour: int
    calls_per_minute: Optional[int] = None


TOOL_RATE_LIMITS: dict[str, dict[str, RateLimit]] = {
    "batch_embed": {
        "free":       RateLimit(calls_per_hour=0),       # blocked
        "user":       RateLimit(calls_per_hour=10, calls_per_minute=2),
        "pro_user":   RateLimit(calls_per_hour=100, calls_per_minute=10),
        "admin":      RateLimit(calls_per_hour=10000),
    },
    "web_search": {
        "free":       RateLimit(calls_per_hour=20, calls_per_minute=5),
        "user":       RateLimit(calls_per_hour=200, calls_per_minute=20),
        "pro_user":   RateLimit(calls_per_hour=2000, calls_per_minute=100),
        "admin":      RateLimit(calls_per_hour=100000),
    },
}


class ToolRateLimiter:
    def __init__(self):
        # {(user_id, tool_name): [timestamps]}
        self._calls: dict[tuple, list[float]] = defaultdict(list)

    def _prune(self, key: tuple, window_seconds: float):
        cutoff = time.monotonic() - window_seconds
        self._calls[key] = [t for t in self._calls[key] if t > cutoff]

    def check(self, user_id: str, role: str, tool_name: str) -> tuple[bool, str]:
        limit = TOOL_RATE_LIMITS.get(tool_name, {}).get(role)
        if limit is None:
            return True, "no limit configured"
        if limit.calls_per_hour == 0:
            return False, f"tool '{tool_name}' is not available on {role} plan"

        key = (user_id, tool_name)

        # Check hourly
        self._prune(key, 3600)
        if len(self._calls[key]) >= limit.calls_per_hour:
            return False, f"hourly limit ({limit.calls_per_hour}/h) exceeded for '{tool_name}'"

        # Check per-minute
        if limit.calls_per_minute is not None:
            minute_key = (user_id, tool_name, "minute")
            self._prune(minute_key, 60)
            if len(self._calls[minute_key]) >= limit.calls_per_minute:
                return False, f"per-minute limit ({limit.calls_per_minute}/m) exceeded for '{tool_name}'"
            self._calls[minute_key].append(time.monotonic())

        self._calls[key].append(time.monotonic())
        return True, "ok"

    async def guarded_call(
        self,
        tool_name: str,
        tool_input: dict,
        user_id: str,
        role: str,
        executor,
    ) -> dict:
        allowed, reason = self.check(user_id, role, tool_name)
        if not allowed:
            raise PermissionError(f"Rate limit: {reason}")
        return await executor(tool_name, tool_input)


# ── Demo ──────────────────────────────────────────────────────────────
async def demo():
    limiter = ToolRateLimiter()

    async def mock_executor(name, args):
        return {"ok": True}

    # Free user blocked from batch_embed
    try:
        await limiter.guarded_call("batch_embed", {}, "free-user", "free", mock_executor)
    except PermissionError as e:
        print(f"BLOCKED: {e}")

    # Pro user allowed
    result = await limiter.guarded_call("batch_embed", {}, "alice", "pro_user", mock_executor)
    print(f"ALLOWED: {result}")


asyncio.run(demo())
```

## Solution 6: Audit trail for permission decisions

Log every allow and deny decision with the user context, tool name, reason, and outcome. Enables security auditing, anomaly detection, and compliance reporting.

```python
import asyncio
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any


@dataclass
class PermissionDecision:
    timestamp: float
    user_id: str
    role: str
    session_id: str
    tool_name: str
    decision: str          # "allow" | "deny"
    reason: str
    tool_input_keys: list[str]  # key names only — not values (avoid logging PII)


class AuditedPermissionGate:
    def __init__(
        self,
        permissions: dict[str, set[str]],
        audit_path: str = "tool_permission_audit.jsonl",
    ):
        self.permissions = permissions
        self.audit_path = Path(audit_path)
        self._decisions: list[PermissionDecision] = []

    def _check(self, user_id: str, role: str, tool_name: str) -> tuple[bool, str]:
        allowed = self.permissions.get(role, set())
        if "*" in allowed or tool_name in allowed:
            return True, "role_allows"
        return False, f"role '{role}' does not include '{tool_name}'"

    def _log(self, decision: PermissionDecision):
        self._decisions.append(decision)
        with open(self.audit_path, "a") as f:
            f.write(json.dumps(asdict(decision)) + "\n")

    async def execute(
        self,
        tool_name: str,
        tool_input: dict,
        user_id: str,
        role: str,
        session_id: str,
        executor,
    ) -> Any:
        allowed, reason = self._check(user_id, role, tool_name)
        decision_obj = PermissionDecision(
            timestamp=time.time(),
            user_id=user_id,
            role=role,
            session_id=session_id,
            tool_name=tool_name,
            decision="allow" if allowed else "deny",
            reason=reason,
            tool_input_keys=list(tool_input.keys()),
        )
        self._log(decision_obj)

        if not allowed:
            raise PermissionError(f"Permission denied: {reason}")

        return await executor(tool_name, tool_input)

    def denied_attempts(self) -> list[PermissionDecision]:
        return [d for d in self._decisions if d.decision == "deny"]

    def top_denied_tools(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for d in self.denied_attempts():
            counts[d.tool_name] = counts.get(d.tool_name, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))


# ── Demo ──────────────────────────────────────────────────────────────
PERMS = {"user": {"web_search"}, "admin": {"*"}}


async def demo():
    gate = AuditedPermissionGate(PERMS)

    async def mock(name, args):
        return {"ok": True}

    calls = [
        ("web_search", {}, "alice", "user"),
        ("delete_record", {}, "alice", "user"),   # denied
        ("run_shell_command", {}, "alice", "user"), # denied
        ("web_search", {}, "bob", "admin"),
    ]
    for tool, args, uid, role in calls:
        try:
            await gate.execute(tool, args, uid, role, "sess-1", mock)
            print(f"ALLOWED: {uid}/{role} → {tool}")
        except PermissionError as e:
            print(f"DENIED: {e}")

    print(f"\nTop denied tools: {gate.top_denied_tools()}")


asyncio.run(demo())
```

## Comparison

| Approach | Granularity | Dynamic rules | Rate limiting | Audit trail | Revocable |
|---|---|---|---|---|---|
| Role-based matrix | Role + tool | No | No | No | No |
| ABAC with dynamic rules | Attribute-based | Yes | No | No | No |
| Agentic loop enforcement | Per tool call | No | No | No | No |
| Scoped capability tokens | Session + tool | No | No | No | Yes (expiry) |
| Per-role rate limiting | Role + quota | No | Yes | No | No |
| Audited permission gate | Role + tool | No | No | Yes | No |

**Recommendation**: Start with **role-based matrix enforcement** (Solution 1) for immediate coverage. Add **per-role rate limiting** (Solution 5) for cost-sensitive tools. Use **scoped capability tokens** (Solution 4) in multi-service architectures where the permission decision should be made once at session start. Wire up the **audited permission gate** (Solution 6) in all production environments to detect anomalies and support compliance reporting.
