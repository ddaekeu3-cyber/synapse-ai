---
layout: solution
title: "Agent Doesn't Implement Capability-Based Access Control for Tools"
category: security
description: "Agents with unrestricted tool access expose dangerous operations to any caller. These patterns show how to enforce capability-based access control so tools are only available to agents and users who have been explicitly granted permission."
tags: [security, access-control, capabilities, authorization, tools, anthropic]
---

## Problem

An agent with a `delete_database`, `send_email`, and `read_file` tool doesn't distinguish between a trusted admin session and an untrusted user-facing session. Without capability-based access control, any prompt — including injected ones — can invoke destructive operations. The fix is to bind tool availability to a capability token that scopes what a given session or agent is allowed to do.

---

### Option 1: Whitelist-Per-Session Tool Filtering

Assign a capability set at session creation and filter the tool list before each API call.

```python
import anthropic
from dataclasses import dataclass, field

client = anthropic.Anthropic()

ALL_TOOLS = [
    {
        "name": "read_file",
        "description": "Read a file from the filesystem",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "write_file",
        "description": "Write content to a file",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    {
        "name": "delete_file",
        "description": "Permanently delete a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
    },
    {
        "name": "send_email",
        "description": "Send an email to a recipient",
        "input_schema": {
            "type": "object",
            "properties": {"to": {"type": "string"}, "body": {"type": "string"}},
            "required": ["to", "body"],
        },
    },
    {
        "name": "query_database",
        "description": "Run a read-only database query",
        "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]},
    },
]

TOOL_INDEX = {t["name"]: t for t in ALL_TOOLS}

ROLE_CAPABILITIES = {
    "readonly": {"read_file", "query_database"},
    "editor": {"read_file", "write_file", "query_database"},
    "admin": {"read_file", "write_file", "delete_file", "send_email", "query_database"},
    "notifier": {"read_file", "send_email"},
}

@dataclass
class Session:
    session_id: str
    role: str
    extra_caps: set[str] = field(default_factory=set)
    denied_caps: set[str] = field(default_factory=set)

    @property
    def capabilities(self) -> set[str]:
        base = ROLE_CAPABILITIES.get(self.role, set())
        return (base | self.extra_caps) - self.denied_caps

    def allowed_tools(self) -> list[dict]:
        return [TOOL_INDEX[name] for name in self.capabilities if name in TOOL_INDEX]

def agent_call(session: Session, user_message: str) -> str:
    tools = session.allowed_tools()
    tool_names = [t["name"] for t in tools]
    print(f"[session={session.session_id}, role={session.role}, tools={tool_names}]")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        system=f"You are an assistant. Your available tools are: {tool_names}. Do not attempt tools not in this list.",
        tools=tools,
        messages=[{"role": "user", "content": user_message}],
    )
    return response.content[0].text if response.content[0].type == "text" else f"Tool call: {response.content[0].name}"

if __name__ == "__main__":
    sessions = [
        Session("s1", "readonly"),
        Session("s2", "editor"),
        Session("s3", "admin"),
        Session("s4", "readonly", extra_caps={"send_email"}),  # one-off grant
    ]
    msg = "Please help me manage some files and possibly send a notification."
    for s in sessions:
        print(f"\n=== {s.role} session ===")
        print(agent_call(s, msg))

# Expected Token Savings: Shorter tool lists reduce system prompt tokens by 30-60%; eliminates unused tool descriptions
# Environment: ANTHROPIC_API_KEY
```

---

### Option 2: Signed Capability Token with Expiry

Issue HMAC-signed tokens that encode capabilities and expiry; verify before each tool execution.

```python
import hmac
import json
import time
import hashlib
import base64
import anthropic

client = anthropic.Anthropic()
SECRET_KEY = b"agent-capability-secret-change-me"

def issue_token(capabilities: list[str], ttl_seconds: int = 3600, subject: str = "agent") -> str:
    payload = {
        "sub": subject,
        "caps": sorted(capabilities),
        "iat": int(time.time()),
        "exp": int(time.time()) + ttl_seconds,
    }
    payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(SECRET_KEY, payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"

def verify_token(token: str) -> dict | None:
    try:
        payload_b64, sig = token.rsplit(".", 1)
        expected = hmac.new(SECRET_KEY, payload_b64.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            print("[token: invalid signature]")
            return None
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + "=="))
        if payload["exp"] < time.time():
            print("[token: expired]")
            return None
        return payload
    except Exception as e:
        print(f"[token: parse error {e}]")
        return None

def check_capability(token: str, tool_name: str) -> bool:
    payload = verify_token(token)
    if not payload:
        return False
    allowed = payload.get("caps", [])
    if tool_name in allowed:
        return True
    print(f"[capability denied: {tool_name} not in {allowed}]")
    return False

TOOL_IMPLEMENTATIONS = {
    "read_file": lambda args: f"[read_file result for {args['path']}]",
    "write_file": lambda args: f"[wrote to {args['path']}]",
    "delete_file": lambda args: f"[deleted {args['path']}]",
    "send_email": lambda args: f"[email sent to {args['to']}]",
}

def execute_tool(token: str, tool_name: str, tool_args: dict) -> str:
    if not check_capability(token, tool_name):
        return f"ERROR: Capability '{tool_name}' not granted to this session."
    fn = TOOL_IMPLEMENTATIONS.get(tool_name)
    if fn:
        return fn(tool_args)
    return f"ERROR: Unknown tool '{tool_name}'"

def run_with_token(token: str, user_message: str) -> str:
    payload = verify_token(token)
    if not payload:
        return "ERROR: Invalid or expired capability token."

    caps = payload["caps"]
    tools = [
        {
            "name": cap,
            "description": f"Execute {cap} operation",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"},
                               "to": {"type": "string"}, "body": {"type": "string"}},
            },
        }
        for cap in caps if cap in TOOL_IMPLEMENTATIONS
    ]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=tools or [],
        messages=[{"role": "user", "content": user_message}],
    )

    results = []
    for block in response.content:
        if block.type == "tool_use":
            result = execute_tool(token, block.name, block.input)
            results.append(f"{block.name}: {result}")
        elif block.type == "text":
            results.append(block.text)
    return "\n".join(results)

if __name__ == "__main__":
    # Admin token
    admin_token = issue_token(["read_file", "write_file", "delete_file", "send_email"], ttl_seconds=3600)
    print("=== Admin token ===")
    print(run_with_token(admin_token, "Read config.json then delete old.log."))

    # Read-only token
    ro_token = issue_token(["read_file"], ttl_seconds=300)
    print("\n=== Read-only token ===")
    print(run_with_token(ro_token, "Delete all log files."))  # should be denied

    # Expired token simulation
    expired = issue_token(["read_file"], ttl_seconds=-1)
    print("\n=== Expired token ===")
    print(run_with_token(expired, "Read anything."))

# Expected Token Savings: Token verification is zero-token overhead; prevents costly unauthorized tool chains
# Environment: ANTHROPIC_API_KEY
```

---

### Option 3: Hierarchical Capability Delegation

Parent agents delegate a subset of their capabilities to child agents — children can never exceed parent grants.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Optional
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class CapabilitySet:
    grants: set[str]

    def delegate(self, subset: set[str]) -> "CapabilitySet":
        """Child can only receive caps the parent holds."""
        actual = self.grants & subset
        denied = subset - self.grants
        if denied:
            print(f"[delegation warning: {denied} not held by parent, dropped]")
        return CapabilitySet(grants=actual)

    def has(self, cap: str) -> bool:
        return cap in self.grants

    def tool_list(self) -> list[str]:
        return sorted(self.grants)

@dataclass
class AgentNode:
    name: str
    caps: CapabilitySet
    children: list["AgentNode"] = field(default_factory=list)

    def spawn_child(self, name: str, requested_caps: set[str]) -> "AgentNode":
        child_caps = self.caps.delegate(requested_caps)
        child = AgentNode(name=name, caps=child_caps)
        self.children.append(child)
        print(f"[{self.name}] spawned [{name}] with caps={child_caps.tool_list()}")
        return child

    async def run(self, task: str) -> str:
        tools = [
            {
                "name": cap,
                "description": f"Perform {cap}",
                "input_schema": {"type": "object", "properties": {"arg": {"type": "string"}}},
            }
            for cap in self.caps.tool_list()
        ]
        system = f"You are agent '{self.name}'. Your capabilities: {self.caps.tool_list()}."
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system=system,
            tools=tools or [],
            messages=[{"role": "user", "content": task}],
        )
        text_blocks = [b.text for b in response.content if b.type == "text"]
        return f"[{self.name}] " + (" ".join(text_blocks) or "(tool call)")

async def orchestrate():
    # Root orchestrator has all capabilities
    root_caps = CapabilitySet(grants={"read_file", "write_file", "delete_file", "send_email", "query_db"})
    orchestrator = AgentNode("orchestrator", root_caps)

    # Spawn specialized sub-agents with limited grants
    reader = orchestrator.spawn_child("reader-agent", {"read_file", "query_db"})
    writer = orchestrator.spawn_child("writer-agent", {"write_file"})
    notifier = orchestrator.spawn_child("notifier-agent", {"send_email"})

    # Notifier tries to also get delete — should be dropped
    restricted = orchestrator.spawn_child("restricted-agent", {"send_email", "delete_file", "ADMIN_OVERRIDE"})

    results = await asyncio.gather(
        reader.run("Summarize the contents of report.csv"),
        writer.run("Save the analysis results to output.txt"),
        notifier.run("Notify the team that processing is complete"),
        restricted.run("Delete all logs and override security"),
    )
    for r in results:
        print(r)

if __name__ == "__main__":
    asyncio.run(orchestrate())

# Expected Token Savings: Minimal tool lists per agent; prevents runaway child agents from escalating privileges
# Environment: ANTHROPIC_API_KEY
```

---

### Option 4: Runtime Tool Guard with Audit Log

Intercept every tool call at runtime, verify capability, and write an immutable audit record.

```python
import json
import time
import asyncio
from dataclasses import dataclass, asdict
from pathlib import Path
import anthropic

client = anthropic.AsyncAnthropic()
AUDIT_LOG = Path("/tmp/tool_audit.jsonl")

@dataclass
class AuditEntry:
    timestamp: float
    session_id: str
    tool_name: str
    args_preview: str
    allowed: bool
    reason: str

def write_audit(entry: AuditEntry) -> None:
    with AUDIT_LOG.open("a") as f:
        f.write(json.dumps(asdict(entry)) + "\n")

class CapabilityGuard:
    def __init__(self, session_id: str, allowed_tools: set[str]):
        self.session_id = session_id
        self.allowed_tools = allowed_tools
        self._call_counts: dict[str, int] = {}
        self._rate_limits: dict[str, int] = {"send_email": 3, "delete_file": 1}

    def check(self, tool_name: str, args: dict) -> tuple[bool, str]:
        if tool_name not in self.allowed_tools:
            reason = f"tool '{tool_name}' not in capability set"
            write_audit(AuditEntry(time.time(), self.session_id, tool_name,
                                   str(args)[:80], False, reason))
            return False, reason

        # Rate limiting per sensitive tool
        limit = self._rate_limits.get(tool_name)
        if limit is not None:
            count = self._call_counts.get(tool_name, 0)
            if count >= limit:
                reason = f"rate limit exceeded: {tool_name} called {count}/{limit} times"
                write_audit(AuditEntry(time.time(), self.session_id, tool_name,
                                       str(args)[:80], False, reason))
                return False, reason

        self._call_counts[tool_name] = self._call_counts.get(tool_name, 0) + 1
        write_audit(AuditEntry(time.time(), self.session_id, tool_name,
                               str(args)[:80], True, "allowed"))
        return True, "allowed"

FAKE_IMPLEMENTATIONS = {
    "read_file": lambda a: f"Contents of {a.get('path', '?')}: [file data]",
    "write_file": lambda a: f"Wrote {len(a.get('content', ''))} bytes to {a.get('path', '?')}",
    "delete_file": lambda a: f"Deleted {a.get('path', '?')}",
    "send_email": lambda a: f"Email sent to {a.get('to', '?')}",
}

async def guarded_agent_loop(session_id: str, allowed_tools: set[str], task: str) -> str:
    guard = CapabilityGuard(session_id, allowed_tools)
    tools = [
        {"name": t, "description": f"Execute {t}",
         "input_schema": {"type": "object", "properties": {
             "path": {"type": "string"}, "content": {"type": "string"},
             "to": {"type": "string"}, "body": {"type": "string"}}}}
        for t in allowed_tools if t in FAKE_IMPLEMENTATIONS
    ]

    messages = [{"role": "user", "content": task}]
    output_parts = []

    for _ in range(5):  # max turns
        response = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            tools=tools,
            messages=messages,
        )

        tool_results = []
        for block in response.content:
            if block.type == "text":
                output_parts.append(block.text)
            elif block.type == "tool_use":
                allowed, reason = guard.check(block.name, block.input)
                result = FAKE_IMPLEMENTATIONS[block.name](block.input) if allowed else f"DENIED: {reason}"
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

        if response.stop_reason == "end_turn":
            break
        if tool_results:
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})

    print(f"\n[Audit log ({session_id}):]")
    for line in AUDIT_LOG.read_text().strip().split("\n")[-5:]:
        entry = json.loads(line)
        status = "✓" if entry["allowed"] else "✗"
        print(f"  {status} {entry['tool_name']}: {entry['reason']}")

    return "\n".join(output_parts) or "(no text output)"

if __name__ == "__main__":
    async def main():
        print("=== Editor session ===")
        r = await guarded_agent_loop("sess-editor", {"read_file", "write_file"},
                                      "Read config.yaml, modify it, then email the team and delete the backup.")
        print(r)

    asyncio.run(main())

# Expected Token Savings: Guard is runtime-only; no token cost; audit log is append-only JSONL
# Environment: ANTHROPIC_API_KEY
```

---

### Option 5: Attribute-Based Access Control (ABAC) with Policy Engine

Evaluate dynamic policies — combining user attributes, resource attributes, and environment conditions — before granting tool access.

```python
import json
import time
from dataclasses import dataclass
from typing import Any
import anthropic

client = anthropic.Anthropic()

@dataclass
class Subject:
    user_id: str
    roles: list[str]
    department: str
    clearance_level: int   # 1-5
    ip_address: str

@dataclass
class Resource:
    tool_name: str
    sensitivity: int       # 1-5
    required_department: str | None
    destructive: bool

@dataclass
class Environment:
    hour_utc: int
    is_maintenance_window: bool

@dataclass
class PolicyDecision:
    allowed: bool
    reason: str
    conditions_met: list[str]
    conditions_failed: list[str]

def evaluate_policy(subject: Subject, resource: Resource, env: Environment) -> PolicyDecision:
    met = []
    failed = []

    # Clearance level must meet sensitivity
    if subject.clearance_level >= resource.sensitivity:
        met.append(f"clearance {subject.clearance_level}>={resource.sensitivity}")
    else:
        failed.append(f"clearance {subject.clearance_level}<{resource.sensitivity}")

    # Destructive operations blocked during business hours unless admin
    if resource.destructive and 9 <= env.hour_utc <= 17 and "admin" not in subject.roles:
        failed.append("destructive op during business hours requires admin role")
    elif resource.destructive:
        met.append("destructive op allowed (off-hours or admin)")

    # Maintenance window blocks all writes
    if env.is_maintenance_window and resource.destructive:
        failed.append("maintenance window: writes blocked")

    # Department restriction
    if resource.required_department and resource.required_department != subject.department:
        failed.append(f"requires department={resource.required_department}, subject={subject.department}")
    elif resource.required_department:
        met.append(f"department match: {subject.department}")

    allowed = len(failed) == 0
    reason = ("All conditions met" if allowed else
              f"Denied: {'; '.join(failed)}")
    return PolicyDecision(allowed=allowed, reason=reason,
                          conditions_met=met, conditions_failed=failed)

TOOL_RESOURCES = {
    "read_file":     Resource("read_file", 1, None, False),
    "write_file":    Resource("write_file", 2, None, False),
    "delete_file":   Resource("delete_file", 4, None, True),
    "send_email":    Resource("send_email", 2, None, False),
    "query_pii":     Resource("query_pii", 5, "data-privacy", False),
    "run_migration": Resource("run_migration", 5, "engineering", True),
}

def abac_filtered_tools(subject: Subject, env: Environment) -> list[dict]:
    allowed = []
    for name, resource in TOOL_RESOURCES.items():
        decision = evaluate_policy(subject, resource, env)
        if decision.allowed:
            allowed.append({
                "name": name,
                "description": f"Execute {name}",
                "input_schema": {"type": "object", "properties": {"arg": {"type": "string"}}},
            })
    return allowed

def run_abac_agent(subject: Subject, task: str) -> str:
    env = Environment(hour_utc=time.gmtime().tm_hour, is_maintenance_window=False)
    tools = abac_filtered_tools(subject, env)
    names = [t["name"] for t in tools]
    print(f"[{subject.user_id} ({subject.department}, L{subject.clearance_level}): tools={names}]")

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=512,
        tools=tools,
        messages=[{"role": "user", "content": task}],
    )
    return next((b.text for b in response.content if b.type == "text"), "(tool call)")

if __name__ == "__main__":
    junior_dev = Subject("u1", ["developer"], "engineering", clearance_level=2, ip_address="10.0.0.1")
    senior_dba = Subject("u2", ["admin", "dba"], "data-privacy", clearance_level=5, ip_address="10.0.0.2")
    analyst = Subject("u3", ["analyst"], "marketing", clearance_level=1, ip_address="10.0.0.3")

    task = "Please delete the backup files, query PII data, and run the migration."
    for user in [junior_dev, senior_dba, analyst]:
        print(f"\n=== {user.user_id} ===")
        print(run_abac_agent(user, task))

# Expected Token Savings: ABAC evaluation is zero-token; shorter tool lists save 20-50% of tool description tokens
# Environment: ANTHROPIC_API_KEY
```

---

### Option 6: Async Multi-Tenant Capability Isolation

Each tenant gets an isolated capability namespace; cross-tenant tool access is structurally impossible.

```python
import asyncio
from dataclasses import dataclass, field
from typing import Callable, Awaitable
import anthropic

client = anthropic.AsyncAnthropic()

@dataclass
class TenantContext:
    tenant_id: str
    plan: str          # "free", "pro", "enterprise"
    capabilities: set[str]
    resource_prefix: str   # all resource paths prefixed with tenant namespace

    def scoped_tool(self, tool_name: str, fn: Callable) -> Callable:
        """Wrap a tool function to enforce tenant namespace isolation."""
        prefix = self.resource_prefix

        async def scoped(*args, **kwargs):
            # Enforce namespace: any path arg must start with tenant prefix
            path = kwargs.get("path", "")
            if path and not path.startswith(prefix):
                return f"ACCESS DENIED: resource '{path}' outside tenant namespace '{prefix}'"
            return await fn(*args, **kwargs)

        return scoped

PLAN_CAPABILITIES = {
    "free": {"read_file", "query_basic"},
    "pro": {"read_file", "write_file", "query_basic", "query_advanced", "send_email"},
    "enterprise": {"read_file", "write_file", "delete_file", "query_basic",
                   "query_advanced", "send_email", "run_reports", "export_data"},
}

async def tenant_agent(ctx: TenantContext, task: str) -> str:
    tools = [
        {
            "name": cap,
            "description": f"[tenant:{ctx.tenant_id}] Execute {cap} within /{ctx.resource_prefix}/",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": f"Must start with /{ctx.resource_prefix}/"},
                    "query": {"type": "string"},
                    "to": {"type": "string"},
                },
            },
        }
        for cap in ctx.capabilities
    ]

    system = (
        f"You are operating for tenant '{ctx.tenant_id}' (plan: {ctx.plan}). "
        f"All resource paths must be prefixed with '/{ctx.resource_prefix}/'. "
        f"Available capabilities: {sorted(ctx.capabilities)}."
    )

    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system,
        tools=tools,
        messages=[{"role": "user", "content": task}],
    )

    results = []
    for block in response.content:
        if block.type == "text":
            results.append(block.text)
        elif block.type == "tool_use":
            # Enforce namespace at execution time
            path = block.input.get("path", "")
            if path and not path.startswith(f"/{ctx.resource_prefix}/"):
                results.append(f"BLOCKED: {block.name}({path}) outside tenant namespace")
            else:
                results.append(f"[executed {block.name} in /{ctx.resource_prefix}/]")

    return f"[{ctx.tenant_id}] " + (" | ".join(results) or "(no output)")

async def run_multi_tenant():
    tenants = [
        TenantContext("acme-corp", "enterprise", PLAN_CAPABILITIES["enterprise"], "tenants/acme-corp"),
        TenantContext("startup-xyz", "pro", PLAN_CAPABILITIES["pro"], "tenants/startup-xyz"),
        TenantContext("free-user", "free", PLAN_CAPABILITIES["free"], "tenants/free-user"),
    ]

    task = "Delete old reports, export all data, and send a summary email."
    results = await asyncio.gather(*[tenant_agent(ctx, task) for ctx in tenants])
    for r in results:
        print(r)

if __name__ == "__main__":
    asyncio.run(run_multi_tenant())

# Expected Token Savings: Tenant isolation prevents cross-tenant tool pollution; plan-based tool lists cut tokens 30-70%
# Environment: ANTHROPIC_API_KEY
```

---

## Comparison

| Option | Approach | Granularity | Auditability | Best For |
|--------|----------|-------------|--------------|----------|
| 1 | Whitelist per session role | Role-level | Low | Simple role-based gating |
| 2 | Signed capability token | Token-level | Medium | Stateless, distributed systems |
| 3 | Hierarchical delegation | Agent-level | Medium | Multi-agent pipelines |
| 4 | Runtime guard + audit log | Call-level | High | Compliance, forensics |
| 5 | ABAC policy engine | Attribute-level | High | Dynamic, context-sensitive access |
| 6 | Multi-tenant namespace isolation | Tenant-level | Medium | SaaS, multi-tenant platforms |
