---
layout: solution
title: "Agent Doesn't Implement RBAC for Tool Access"
category: auth
description: "Agents that give all users access to all tools allow low-privilege users to trigger destructive actions — role-based access control restricts which tools each caller can invoke."
tags: [auth, rbac, tool-access, permissions, security, authorization, multi-tenant]
---

# Agent Doesn't Implement RBAC for Tool Access

## Problem

An agent's tool list represents its capability surface. Without role-based access control, a read-only user can invoke `delete_record`, a guest can run `execute_code`, and an anonymous caller can trigger `send_email`. These mistakes cause real harm: data deletion, spam, resource exhaustion, and privilege escalation. RBAC for tool access means: before letting the model see or call a tool, verify that the caller's role is permitted to use it.

## Solutions

### Option 1: Tool Allowlist Per Role

Define an allowlist of permitted tools for each role. Filter the tool list before passing it to the model — the model can't call what it can't see.

```python
import anthropic
from typing import Literal
from dataclasses import dataclass

client = anthropic.Anthropic()

# All available tools
ALL_TOOLS = [
    {
        "name": "read_record",
        "description": "Read a record from the database by ID.",
        "input_schema": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
    {
        "name": "write_record",
        "description": "Create or update a record in the database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "record_id": {"type": "string"},
                "data": {"type": "object"},
            },
            "required": ["record_id", "data"],
        },
    },
    {
        "name": "delete_record",
        "description": "Permanently delete a record from the database.",
        "input_schema": {
            "type": "object",
            "properties": {"record_id": {"type": "string"}},
            "required": ["record_id"],
        },
    },
    {
        "name": "send_email",
        "description": "Send an email to a user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "execute_code",
        "description": "Execute arbitrary Python code in a sandbox.",
        "input_schema": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
]

# Role → allowed tool names
ROLE_TOOL_ALLOWLIST: dict[str, set[str]] = {
    "guest":   {"read_record"},
    "user":    {"read_record", "write_record"},
    "manager": {"read_record", "write_record", "send_email"},
    "admin":   {"read_record", "write_record", "delete_record", "send_email", "execute_code"},
}

@dataclass
class Caller:
    user_id: str
    role: str

def get_tools_for_role(role: str) -> list[dict]:
    allowed = ROLE_TOOL_ALLOWLIST.get(role, set())
    return [t for t in ALL_TOOLS if t["name"] in allowed]

def rbac_agent_call(caller: Caller, user_message: str) -> dict:
    tools = get_tools_for_role(caller.role)

    if not tools:
        return {
            "error": f"Role '{caller.role}' has no tool access",
            "answer": None,
        }

    # Model only sees tools the caller is allowed to use
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        system=f"You are an assistant for user {caller.user_id} (role: {caller.role}). "
               f"You have access to {len(tools)} tool(s) appropriate for your role.",
        tools=tools,
        messages=[{"role": "user", "content": user_message}],
    )

    tool_calls = [
        b.name for b in resp.content if hasattr(b, "name") and b.type == "tool_use"
    ]
    answer = next((b.text for b in resp.content if hasattr(b, "text")), "")

    return {
        "role": caller.role,
        "tools_available": [t["name"] for t in tools],
        "tool_calls_attempted": tool_calls,
        "answer": answer,
    }

# Test different roles
for role in ["guest", "user", "manager", "admin"]:
    caller = Caller(user_id=f"user_{role}", role=role)
    result = rbac_agent_call(caller, "Delete record #42 and send a confirmation email.")
    print(f"[{role:8}] tools={result['tools_available']}")
    print(f"          attempted={result['tool_calls_attempted']}")
# Expected Token Savings: Smaller tool lists = fewer tokens in every request
# Environment: Multi-tenant SaaS agents, enterprise chatbots, API-exposed agents
```

### Option 2: Tool Call Interception with Runtime Enforcement

Even if the model generates a tool call, intercept it before execution and reject calls not in the caller's allowlist. Defense-in-depth beyond tool list filtering.

```python
import anthropic
import json
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class AuthContext:
    user_id: str
    role: str
    session_id: str

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "readonly": {"search_records", "get_record"},
    "editor":   {"search_records", "get_record", "update_record", "create_record"},
    "admin":    {"search_records", "get_record", "update_record", "create_record", "delete_record"},
}

class RBACViolationError(Exception):
    pass

def check_tool_permission(auth: AuthContext, tool_name: str):
    """Raises RBACViolationError if the caller cannot use this tool."""
    allowed = ROLE_PERMISSIONS.get(auth.role, set())
    if tool_name not in allowed:
        raise RBACViolationError(
            f"User '{auth.user_id}' (role='{auth.role}') "
            f"is not permitted to call tool '{tool_name}'. "
            f"Allowed tools: {sorted(allowed)}"
        )

# Mock tool implementations
def execute_tool(name: str, args: dict, auth: AuthContext) -> str:
    # Runtime permission check — second layer of defense
    check_tool_permission(auth, name)

    if name == "get_record":
        return json.dumps({"id": args.get("id"), "data": "sample data", "owner": auth.user_id})
    elif name == "search_records":
        return json.dumps({"results": [], "query": args.get("query", "")})
    elif name == "update_record":
        return json.dumps({"updated": True, "id": args.get("id")})
    elif name == "create_record":
        return json.dumps({"created": True, "id": "new_id_123"})
    elif name == "delete_record":
        return json.dumps({"deleted": True, "id": args.get("id")})
    return json.dumps({"error": "unknown tool"})

FULL_TOOL_LIST = [
    {"name": "search_records", "description": "Search all records.",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "get_record", "description": "Get a record by ID.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "update_record", "description": "Update a record.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}, "data": {"type": "object"}}, "required": ["id", "data"]}},
    {"name": "create_record", "description": "Create a new record.",
     "input_schema": {"type": "object", "properties": {"data": {"type": "object"}}, "required": ["data"]}},
    {"name": "delete_record", "description": "Delete a record permanently.",
     "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
]

def intercepted_agent(auth: AuthContext, user_message: str) -> dict:
    # Layer 1: Show only allowed tools to the model
    allowed_names = ROLE_PERMISSIONS.get(auth.role, set())
    visible_tools = [t for t in FULL_TOOL_LIST if t["name"] in allowed_names]

    messages = [{"role": "user", "content": user_message}]
    audit_log = []

    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            tools=visible_tools,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            answer = next((b.text for b in resp.content if hasattr(b, "text")), "")
            return {"answer": answer, "audit_log": audit_log}

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []

            for block in resp.content:
                if block.type != "tool_use":
                    continue

                try:
                    # Layer 2: Runtime enforcement even if model somehow calls wrong tool
                    result_str = execute_tool(block.name, block.input, auth)
                    status = "allowed"
                except RBACViolationError as e:
                    result_str = json.dumps({"error": "Permission denied", "detail": str(e)})
                    status = "denied"

                audit_log.append({
                    "tool": block.name,
                    "user": auth.user_id,
                    "role": auth.role,
                    "status": status,
                })
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_str,
                })

            messages.append({"role": "user", "content": tool_results})

auth = AuthContext("alice", "readonly", "sess_001")
result = intercepted_agent(auth, "Find and delete record ID abc-123.")
print(f"Answer: {result['answer'][:150]}")
print(f"Audit: {result['audit_log']}")
# Expected Token Savings: Smaller visible tool list + no wasted delete calls
# Environment: Any multi-tenant agent with mixed privilege users
```

### Option 3: Attribute-Based Tool Access Control (ABAC)

Beyond roles, restrict tool access based on resource attributes — users can only call tools on resources they own.

```python
import anthropic
import json
from dataclasses import dataclass
from typing import Callable, Any

client = anthropic.Anthropic()

@dataclass
class Principal:
    user_id: str
    role: str
    tenant_id: str

@dataclass
class ToolPolicy:
    tool_name: str
    required_role: str
    resource_check: Callable[[Principal, dict], bool] | None = None

# Policies: role requirement + optional resource ownership check
TOOL_POLICIES = [
    ToolPolicy("read_document",   required_role="user"),
    ToolPolicy("update_document", required_role="user",
               resource_check=lambda p, args: args.get("owner_id") == p.user_id),
    ToolPolicy("delete_document", required_role="admin",
               resource_check=lambda p, args: args.get("tenant_id") == p.tenant_id),
    ToolPolicy("list_all_users",  required_role="admin"),
    ToolPolicy("export_data",     required_role="manager"),
]

ROLE_HIERARCHY = {"admin": 3, "manager": 2, "user": 1, "guest": 0}

def has_sufficient_role(principal: Principal, required_role: str) -> bool:
    caller_level = ROLE_HIERARCHY.get(principal.role, 0)
    required_level = ROLE_HIERARCHY.get(required_role, 999)
    return caller_level >= required_level

def check_abac(principal: Principal, tool_name: str, args: dict) -> tuple[bool, str]:
    policy = next((p for p in TOOL_POLICIES if p.tool_name == tool_name), None)

    if policy is None:
        return False, f"No policy defined for tool '{tool_name}'"

    if not has_sufficient_role(principal, policy.required_role):
        return False, (
            f"Role '{principal.role}' insufficient for '{tool_name}' "
            f"(requires '{policy.required_role}')"
        )

    if policy.resource_check and not policy.resource_check(principal, args):
        return False, (
            f"Resource ownership check failed for '{tool_name}': "
            f"user '{principal.user_id}' cannot access this resource"
        )

    return True, ""

ABAC_TOOLS = [
    {
        "name": "read_document",
        "description": "Read a document.",
        "input_schema": {"type": "object", "properties": {"doc_id": {"type": "string"}}, "required": ["doc_id"]},
    },
    {
        "name": "update_document",
        "description": "Update a document you own.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "owner_id": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["doc_id", "owner_id", "content"],
        },
    },
    {
        "name": "delete_document",
        "description": "Delete a document (admin only, same tenant).",
        "input_schema": {
            "type": "object",
            "properties": {"doc_id": {"type": "string"}, "tenant_id": {"type": "string"}},
            "required": ["doc_id", "tenant_id"],
        },
    },
]

def abac_agent(principal: Principal, user_message: str) -> dict:
    # Visible tools = those where role is sufficient
    visible = [
        t for t in ABAC_TOOLS
        if has_sufficient_role(principal, next(
            (p.required_role for p in TOOL_POLICIES if p.tool_name == t["name"]), "admin"
        ))
    ]

    messages = [{"role": "user", "content": user_message}]
    decisions = []

    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            tools=visible,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            return {
                "answer": next((b.text for b in resp.content if hasattr(b, "text")), ""),
                "decisions": decisions,
            }

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                allowed, reason = check_abac(principal, block.name, block.input)
                decisions.append({"tool": block.name, "allowed": allowed, "reason": reason or "ok"})
                content = (
                    json.dumps({"result": f"Executed {block.name}"})
                    if allowed
                    else json.dumps({"error": "Access denied", "reason": reason})
                )
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": content})
            messages.append({"role": "user", "content": results})

# User tries to update another user's document
p = Principal("alice", "user", "tenant_acme")
result = abac_agent(p, "Update document doc_xyz (owned by bob) with new content.")
print(f"ABAC decisions: {result['decisions']}")
print(f"Answer: {result['answer'][:150]}")
# Expected Token Savings: Fine-grained filtering prevents unauthorized multi-step tool chains
# Environment: Document management, CRM agents, multi-tenant enterprise platforms
```

### Option 4: Tool Scoping via System Prompt Trust Separation

Encode tool permissions directly in the system prompt so the model understands its own access constraints — works even without code-level enforcement.

```python
import anthropic

client = anthropic.Anthropic()

TOOL_DEFINITIONS = {
    "search_kb": {
        "description": "Search the knowledge base.",
        "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
    },
    "create_ticket": {
        "description": "Create a support ticket.",
        "input_schema": {"type": "object", "properties": {"title": {"type": "string"}, "description": {"type": "string"}}, "required": ["title", "description"]},
    },
    "close_ticket": {
        "description": "Close a support ticket.",
        "input_schema": {"type": "object", "properties": {"ticket_id": {"type": "string"}, "resolution": {"type": "string"}}, "required": ["ticket_id", "resolution"]},
    },
    "escalate_ticket": {
        "description": "Escalate a ticket to tier-2 support.",
        "input_schema": {"type": "object", "properties": {"ticket_id": {"type": "string"}}, "required": ["ticket_id"]},
    },
    "refund_order": {
        "description": "Issue a refund for an order.",
        "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}, "amount": {"type": "number"}}, "required": ["order_id", "amount"]},
    },
}

ROLE_CONFIGS = {
    "tier1_support": {
        "tools": ["search_kb", "create_ticket", "escalate_ticket"],
        "system_addendum": (
            "You are a Tier-1 support agent. "
            "You CANNOT close tickets (only Tier-2 can). "
            "You CANNOT issue refunds (only managers can). "
            "Escalate complex issues using escalate_ticket."
        ),
    },
    "tier2_support": {
        "tools": ["search_kb", "create_ticket", "close_ticket", "escalate_ticket"],
        "system_addendum": (
            "You are a Tier-2 support agent. "
            "You CAN close tickets. "
            "You CANNOT issue refunds (only managers can). "
        ),
    },
    "manager": {
        "tools": list(TOOL_DEFINITIONS.keys()),
        "system_addendum": (
            "You are a Support Manager with full tool access including refunds. "
            "Use refund_order only when clearly justified."
        ),
    },
}

BASE_SYSTEM = "You are a customer support assistant. Help users resolve their issues."

def support_agent(role: str, user_message: str) -> dict:
    config = ROLE_CONFIGS.get(role)
    if not config:
        return {"error": f"Unknown role: {role}"}

    tools = [
        {"name": name, **TOOL_DEFINITIONS[name]}
        for name in config["tools"]
    ]
    system = f"{BASE_SYSTEM}\n\n{config['system_addendum']}"

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=system,
        tools=tools,
        messages=[{"role": "user", "content": user_message}],
    )

    tool_calls = [b.name for b in resp.content if hasattr(b, "type") and b.type == "tool_use"]
    answer = next((b.text for b in resp.content if hasattr(b, "text")), "")

    return {
        "role": role,
        "tools_available": config["tools"],
        "tool_calls": tool_calls,
        "answer": answer[:150],
    }

for role in ["tier1_support", "tier2_support", "manager"]:
    result = support_agent(role, "Customer wants a refund and to close their ticket.")
    print(f"[{role:16}] available={result['tools_available']}")
    print(f"                  called={result['tool_calls']}")
# Expected Token Savings: Role-scoped tool lists are 40-80% smaller → fewer input tokens
# Environment: Customer support agents, help desk automation, tiered-access workflows
```

### Option 5: Dynamic Permission Resolution with JWT Claims

Extract tool permissions from JWT claims at runtime — permissions are issued with the auth token, not hard-coded in the agent.

```python
import anthropic
import json
import base64
import hmac
import hashlib
import time
from dataclasses import dataclass

client = anthropic.Anthropic()

SECRET = b"demo-jwt-secret"

def create_token(user_id: str, role: str, allowed_tools: list[str], ttl: int = 3600) -> str:
    """Create a simple demo JWT-like token."""
    payload = {
        "sub": user_id,
        "role": role,
        "tools": allowed_tools,
        "exp": int(time.time()) + ttl,
        "iat": int(time.time()),
    }
    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256"}).encode()).decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig_input = f"{header}.{body}".encode()
    sig = base64.urlsafe_b64encode(
        hmac.new(SECRET, sig_input, hashlib.sha256).digest()
    ).decode()
    return f"{header}.{body}.{sig}"

def decode_token(token: str) -> dict:
    """Decode and verify token. Returns claims."""
    try:
        header, body, sig = token.split(".")
        sig_input = f"{header}.{body}".encode()
        expected = base64.urlsafe_b64encode(
            hmac.new(SECRET, sig_input, hashlib.sha256).digest()
        ).decode()
        if not hmac.compare_digest(sig, expected):
            raise ValueError("Invalid token signature")
        claims = json.loads(base64.urlsafe_b64decode(body + "=="))
        if claims.get("exp", 0) < time.time():
            raise ValueError("Token expired")
        return claims
    except Exception as e:
        raise ValueError(f"Token validation failed: {e}")

TOOL_CATALOG = [
    {"name": "read_data",   "description": "Read data records.", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "write_data",  "description": "Write data records.", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}, "value": {"type": "string"}}, "required": ["id", "value"]}},
    {"name": "delete_data", "description": "Delete data records.", "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}},
    {"name": "admin_action","description": "Perform admin operations.", "input_schema": {"type": "object", "properties": {"action": {"type": "string"}}, "required": ["action"]}},
]

def jwt_rbac_agent(auth_token: str, user_message: str) -> dict:
    try:
        claims = decode_token(auth_token)
    except ValueError as e:
        return {"error": f"Auth failed: {e}"}

    allowed_tools = set(claims.get("tools", []))
    visible_tools = [t for t in TOOL_CATALOG if t["name"] in allowed_tools]

    if not visible_tools:
        return {"error": "Token grants no tool access"}

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=200,
        system=f"You assist user '{claims['sub']}' (role: {claims['role']}). "
               f"You have access to {len(visible_tools)} tool(s) per their JWT permissions.",
        tools=visible_tools,
        messages=[{"role": "user", "content": user_message}],
    )

    return {
        "user": claims["sub"],
        "role": claims["role"],
        "granted_tools": list(allowed_tools),
        "tool_calls": [b.name for b in resp.content if hasattr(b, "type") and b.type == "tool_use"],
        "answer": next((b.text for b in resp.content if hasattr(b, "text")), "")[:150],
    }

# Issue tokens with different permission scopes
reader_token  = create_token("alice", "reader",  ["read_data"])
editor_token  = create_token("bob",   "editor",  ["read_data", "write_data"])
admin_token   = create_token("carol", "admin",   ["read_data", "write_data", "delete_data", "admin_action"])

for token, label in [(reader_token, "reader"), (editor_token, "editor"), (admin_token, "admin")]:
    result = jwt_rbac_agent(token, "Delete record #99 and run admin maintenance.")
    print(f"[{label:6}] granted={result.get('granted_tools', [])} called={result.get('tool_calls', [])}")
# Expected Token Savings: JWT-scoped tools shrink tool list per caller; no hardcoded role maps
# Environment: API gateways, OAuth-integrated agents, token-based microservices
```

### Option 6: Tool Access Audit Logging

Log every tool access attempt (allowed and denied) for compliance, anomaly detection, and RBAC policy tuning.

```python
import anthropic
import json
import sqlite3
import time
from datetime import datetime
from dataclasses import dataclass

client = anthropic.Anthropic()

@dataclass
class AuditEvent:
    event_id: str
    user_id: str
    role: str
    tool_name: str
    action: str       # "allowed" | "denied" | "not_in_list"
    reason: str
    tool_args_hash: str
    timestamp: str

class ToolAccessAuditor:
    def __init__(self, db_path: str = ":memory:"):
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS tool_audit (
                event_id TEXT PRIMARY KEY,
                user_id TEXT,
                role TEXT,
                tool_name TEXT,
                action TEXT,
                reason TEXT,
                args_hash TEXT,
                timestamp TEXT
            )
        """)
        self.conn.commit()

    def log(self, event: AuditEvent):
        self.conn.execute(
            "INSERT INTO tool_audit VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (event.event_id, event.user_id, event.role, event.tool_name,
             event.action, event.reason, event.tool_args_hash, event.timestamp)
        )
        self.conn.commit()

    def get_recent(self, n: int = 10) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM tool_audit ORDER BY timestamp DESC LIMIT ?", (n,)
        ).fetchall()
        cols = ["event_id", "user_id", "role", "tool_name", "action", "reason", "args_hash", "timestamp"]
        return [dict(zip(cols, r)) for r in rows]

    def get_denied_events(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT user_id, role, tool_name, reason, timestamp FROM tool_audit WHERE action='denied'"
        ).fetchall()
        return [{"user_id": r[0], "role": r[1], "tool": r[2], "reason": r[3], "when": r[4]} for r in rows]

auditor = ToolAccessAuditor()

RBAC = {
    "analyst": {"read_data", "query_db"},
    "engineer": {"read_data", "query_db", "deploy_service"},
    "admin": {"read_data", "query_db", "deploy_service", "delete_data"},
}

AUDITED_TOOLS = [
    {"name": "read_data",       "description": "Read data.", "input_schema": {"type": "object", "properties": {"table": {"type": "string"}}, "required": ["table"]}},
    {"name": "query_db",        "description": "Run a query.", "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "deploy_service",  "description": "Deploy a service.", "input_schema": {"type": "object", "properties": {"service": {"type": "string"}}, "required": ["service"]}},
    {"name": "delete_data",     "description": "Delete data.", "input_schema": {"type": "object", "properties": {"table": {"type": "string"}, "id": {"type": "string"}}, "required": ["table", "id"]}},
]

import uuid
import hashlib

def audited_agent(user_id: str, role: str, user_message: str) -> dict:
    allowed = RBAC.get(role, set())
    visible_tools = [t for t in AUDITED_TOOLS if t["name"] in allowed]

    messages = [{"role": "user", "content": user_message}]

    while True:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=250,
            tools=visible_tools,
            messages=messages,
        )

        if resp.stop_reason == "end_turn":
            return {"answer": next((b.text for b in resp.content if hasattr(b, "text")), "")}

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue

                args_hash = hashlib.sha256(json.dumps(block.input, sort_keys=True).encode()).hexdigest()[:12]

                if block.name in allowed:
                    action, reason, result_content = "allowed", "in_role_allowlist", json.dumps({"ok": True})
                else:
                    action, reason = "denied", f"role '{role}' cannot call '{block.name}'"
                    result_content = json.dumps({"error": "Access denied"})

                auditor.log(AuditEvent(
                    event_id=str(uuid.uuid4())[:8],
                    user_id=user_id,
                    role=role,
                    tool_name=block.name,
                    action=action,
                    reason=reason,
                    tool_args_hash=args_hash,
                    timestamp=datetime.now().isoformat(),
                ))
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result_content})

            messages.append({"role": "user", "content": tool_results})

audited_agent("alice", "analyst", "Read users table and delete inactive records.")
audited_agent("bob",   "admin",   "Deploy auth-service and clean up old data.")

print("\nAll audit events:")
for ev in auditor.get_recent():
    icon = "✓" if ev["action"] == "allowed" else "✗"
    print(f"  {icon} [{ev['role']:8}] {ev['tool_name']:20} → {ev['action']}")

print("\nDenied events:")
for ev in auditor.get_denied_events():
    print(f"  {ev['user_id']} ({ev['role']}) tried {ev['tool']}: {ev['reason']}")
# Expected Token Savings: Audit trails enable policy tightening that reduces tool surface over time
# Environment: Compliance-regulated environments, SOC 2, HIPAA, enterprise security
```

## Comparison Table

| Option | Enforcement Layer | Granularity | Overhead | Best For |
|--------|------------------|------------|----------|----------|
| 1: Tool Allowlist Per Role | Model visibility | Role-level | None | Simple role hierarchies |
| 2: Runtime Interception | Pre-execution check | Per-call | Minimal | Defense-in-depth security |
| 3: ABAC with Resource Checks | Model + runtime | User + resource | Low | Multi-tenant resource ownership |
| 4: System Prompt Trust | Model instruction | Role-level | None | Quick deployment, low-code enforcement |
| 5: JWT Claim Permissions | Token-driven | Per-token | Token decode | OAuth/API gateway integrations |
| 6: Audit Logging | Post-call tracking | Full detail | DB write | Compliance, anomaly detection, policy tuning |
